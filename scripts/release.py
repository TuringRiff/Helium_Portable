"""Track upstream Helium and publish one GitHub Release per version.

Two subcommands sharing a single definition of the release tag:

  check    Resolve the upstream Helium version and decide whether it still needs
           building. Writes UPDATE_NEEDED / PACKAGE_VERSION / RELEASE_TAG.
  publish  Attach the freshly built archive to that version's release, creating
           the release if it does not exist yet.

Helium's package version (0.15.4.1) is not the bundled Chromium version
(140.0.x.y) that the builder treats as `{version}`, so the tag, title and archive
name all read `{package_version}`. That same mismatch is why the builder's
`render-release` / `update-release` commands are unused here.

`check` asks GitHub whether the tag's release exists instead of regex-matching
the newest release's Markdown body the way the builder's `check` does. With one
release per version the tag *is* the record of what has been published, so a
single lookup answers the question exactly: reworded release notes can no longer
make every scheduled run rebuild, and a lower-numbered upstream hotfix is no
longer mistaken for "already published" because some newer release sits on top.
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from string import Formatter
from urllib.parse import quote

import requests

try:
    from portable_builder.builder import build_context, format_value, get_version_info
    from portable_builder.config import get_target, load_config
    from portable_builder.github_env import write_env
    from portable_builder.release import archive_name_regex
    from portable_builder.tools import configure_stdout
    from portable_builder.versions import is_upgrade
except ImportError as exc:
    raise SystemExit(
        "portable_builder is not importable. Point PYTHONPATH at the ChromiumPortable "
        "checkout (CI uses _portable_builder, locally ..\\ChromiumPortable)."
    ) from exc

API_ROOT = "https://api.github.com"
UPLOAD_ROOT = "https://uploads.github.com"
UPLOAD_ATTEMPTS = 3

# Fields resolvable before the build runs. The tag must be a pure function of the
# version: one that also depended on {date} or the Chromium {version} would differ
# between `check` and `publish`, and a rebuild of the same Helium version would
# land on a second tag instead of updating the first.
TAG_FIELDS = ("target", "name", "display_name", "output_dir", "package_version", "arch")


def api_headers(require_token=True):
    token = os.getenv("GITHUB_TOKEN")
    if require_token and not token:
        raise SystemExit("GITHUB_TOKEN is not set.")

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Helium_Portable release script",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def require_repo():
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is not set; publishing must run inside GitHub Actions.")
    return repo


def template_fields(*templates):
    fields = set()
    for template in templates:
        for _, field_name, _, _ in Formatter().parse(str(template)):
            if field_name:
                fields.add(field_name)
    return fields


def render_tag(target, package_version):
    context = {
        "target": target.get("target", ""),
        "name": target.get("name", target.get("display_name", "")),
        "display_name": target.get("display_name", target.get("name", "")),
        "output_dir": target.get("output_dir", target.get("name", "Browser")),
        "package_version": package_version,
        "arch": target.get("architecture", "x64"),
    }
    template = target.get("release", {}).get("tag", "v{package_version}")
    unknown = template_fields(template) - set(TAG_FIELDS)
    if unknown:
        allowed = ", ".join("{%s}" % name for name in TAG_FIELDS)
        raise SystemExit(
            f"release.tag template {template!r} uses {sorted(unknown)}, which is unknown at check "
            f"time and would let check and publish disagree. Allowed fields: {allowed}."
        )
    return format_value(template, context)


def get_release_by_tag(repo, tag):
    response = requests.get(
        f"{API_ROOT}/repos/{repo}/releases/tags/{quote(tag, safe='')}",
        headers=api_headers(require_token=False),
        timeout=60,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def find_draft_by_tag(repo, tag):
    """Drafts keep their tag_name but stay invisible to /releases/tags.

    Without this lookup an upload that died mid-flight would leave a draft behind
    and the next run would stack a second one on the same tag.
    """
    response = requests.get(
        f"{API_ROOT}/repos/{repo}/releases",
        params={"per_page": 100},
        headers=api_headers(),
        timeout=60,
    )
    response.raise_for_status()
    return next(
        (item for item in response.json() if item.get("draft") and item.get("tag_name") == tag),
        None,
    )


def matching_asset_names(release, pattern):
    return [
        asset.get("name", "")
        for asset in (release or {}).get("assets", [])
        if pattern.fullmatch(asset.get("name", ""))
    ]


def delete_asset(repo, asset_id):
    response = requests.delete(
        f"{API_ROOT}/repos/{repo}/releases/assets/{asset_id}",
        headers=api_headers(),
        timeout=60,
    )
    if response.status_code not in (204, 404):
        raise SystemExit(f"Failed to delete asset {asset_id}: {response.status_code} {response.text}")


def patch_release(repo, release_id, payload):
    response = requests.patch(
        f"{API_ROOT}/repos/{repo}/releases/{release_id}",
        headers=api_headers(),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def create_draft_release(repo, tag, title, body):
    payload = {
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": True,
        "prerelease": False,
    }
    # Pin the tag to the commit that produced this build rather than whatever
    # happens to be on the default branch when the tag is finally created.
    commitish = os.getenv("GITHUB_SHA")
    if commitish:
        payload["target_commitish"] = commitish

    response = requests.post(
        f"{API_ROOT}/repos/{repo}/releases",
        headers=api_headers(),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def upload_archive(repo, release, archive):
    # GitHub rejects a duplicate asset name, so a same-day rebuild has to drop the
    # previous copy first.
    for asset in release.get("assets", []):
        if asset.get("name") == archive.name:
            print(f"[INFO] Replacing existing asset: {archive.name}")
            delete_asset(repo, asset["id"])

    url = f"{UPLOAD_ROOT}/repos/{repo}/releases/{release['id']}/assets?name={quote(archive.name, safe='')}"
    headers = {**api_headers(), "Content-Type": "application/octet-stream"}
    failure = None

    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            with archive.open("rb") as file:
                response = requests.post(url, data=file, headers=headers, timeout=1800)
            if response.status_code in (200, 201):
                print(f"[INFO] Uploaded {archive.name} on attempt {attempt}.")
                return response.json()
            failure = f"{response.status_code} {response.text}"
        except requests.RequestException as exc:
            failure = str(exc)

        if attempt < UPLOAD_ATTEMPTS:
            delay = 5 * attempt
            print(f"[WARN] Upload attempt {attempt} failed ({failure}); retrying in {delay}s.")
            time.sleep(delay)
            # A timeout can still have landed the asset server-side; clear it so the
            # retry does not collide with a half-written copy of itself.
            current = get_release_by_tag(repo, release["tag_name"]) or {}
            for asset in current.get("assets", []):
                if asset.get("name") == archive.name:
                    delete_asset(repo, asset["id"])

    raise SystemExit(f"Failed to upload {archive.name} after {UPLOAD_ATTEMPTS} attempts: {failure}")


def prune_superseded_assets(repo, release, pattern, keep_name):
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name != keep_name and pattern.fullmatch(name):
            print(f"[INFO] Removing superseded asset: {name}")
            delete_asset(repo, asset["id"])


def should_take_latest(repo, package_version):
    """Keep a rebuild of an older version from stealing the `latest` badge."""
    response = requests.get(
        f"{API_ROOT}/repos/{repo}/releases/latest",
        headers=api_headers(require_token=False),
        timeout=60,
    )
    if response.status_code == 404:
        return True
    response.raise_for_status()

    current = str(response.json().get("tag_name", "")).lstrip("vV")
    return not current or not is_upgrade(current, package_version)


def find_archive(workdir, target, pattern):
    asset_path = os.getenv("ASSET_PATH")
    if asset_path:
        path = Path(asset_path)
        if not path.is_absolute():
            path = workdir / path
        if not path.exists():
            raise SystemExit(f"ASSET_PATH does not exist: {path}")
        return path

    assets_dir = workdir / "build" / "assets"
    matches = [path for path in assets_dir.glob("*.7z") if pattern.fullmatch(path.name)]
    if not matches:
        raise SystemExit(
            f"No archive matching {target.get('archive_name')!r} found in {assets_dir}; "
            "run the archive stage first."
        )
    return max(matches, key=lambda path: path.stat().st_mtime)


def command_check(target, workdir):
    package = get_version_info(target, workdir)
    package_version = package["version"]
    tag = render_tag(target, package_version)
    pattern = archive_name_regex(target)

    repo = os.getenv("GITHUB_REPOSITORY")
    if repo:
        release = get_release_by_tag(repo, tag)
    else:
        release = None
        print("[INFO] GITHUB_REPOSITORY is not set; assuming a local run with nothing published.")

    published_assets = matching_asset_names(release, pattern)

    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        update_needed, reason = True, "manual dispatch forces a rebuild"
    elif release is None:
        update_needed, reason = True, f"no published release for {tag}"
    elif not published_assets:
        update_needed, reason = True, f"{tag} exists but carries no matching archive"
    else:
        update_needed, reason = False, f"{tag} is already published as {published_assets[0]}"

    print(f"[INFO] Upstream Helium version: {package_version}")
    print(f"[INFO] Release tag: {tag}")
    print(f"[INFO] update_needed={str(update_needed).lower()} ({reason})")

    write_env(
        {
            "UPDATE_NEEDED": str(update_needed).lower(),
            "UPSTREAM_VERSION": package_version,
            "PACKAGE_VERSION": package_version,
            "RELEASE_TAG": tag,
        }
    )


def command_publish(target, workdir):
    repo = require_repo()
    api_headers()

    package_version = os.getenv("PACKAGE_VERSION") or os.getenv("UPSTREAM_VERSION")
    if not package_version:
        raise SystemExit("PACKAGE_VERSION/UPSTREAM_VERSION is not set; run the build stage first.")

    chromium_version = os.getenv("BUILT_VERSION") or os.getenv("BROWSER_VERSION") or ""
    build_date = os.getenv("BUILD_DATE") or datetime.now().strftime("%Y-%m-%d")

    release_config = target.get("release", {})
    title_template = release_config.get("title", "{display_name} {package_version}")
    body_template = release_config.get("body", "")

    if "version" in template_fields(title_template, body_template) and not chromium_version:
        raise SystemExit(
            "Release templates reference {version} (the bundled Chromium version) but BUILT_VERSION "
            "is unset; run the build stage first, or switch the template to {package_version}."
        )

    context = build_context(
        target,
        version=chromium_version,
        date=build_date,
        package_version=package_version,
    )
    tag = render_tag(target, package_version)
    title = format_value(title_template, context)
    body = format_value(body_template, context)

    pattern = archive_name_regex(target)
    archive = find_archive(workdir, target, pattern)

    print(f"[INFO] Publishing {tag} ({title})")
    print(f"[INFO] Archive: {archive.name} ({archive.stat().st_size} bytes)")

    release = get_release_by_tag(repo, tag) or find_draft_by_tag(repo, tag)
    if release:
        state = "draft" if release.get("draft") else "published"
        print(f"[INFO] Reusing the existing {state} release for {tag}.")
        patch_release(repo, release["id"], {"name": title, "body": body})
    else:
        # Publish only once the archive is attached, so the tag never points at a
        # release users can see but cannot download from.
        release = create_draft_release(repo, tag, title, body)
        print(f"[INFO] Created draft release {tag}.")

    upload_archive(repo, release, archive)
    prune_superseded_assets(repo, release, pattern, archive.name)

    if release.get("draft"):
        payload = {"draft": False}
        if should_take_latest(repo, package_version):
            payload["make_latest"] = "true"
        else:
            print("[INFO] A newer release already exists; leaving the latest badge alone.")
        patch_release(repo, release["id"], payload)

    print(f"[INFO] Published {tag} with {archive.name}.")


def main():
    configure_stdout()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="browser.json")
    parser.add_argument("--target", default="helium_stable")
    parser.add_argument("--workdir", default=".")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Decide whether the upstream version still needs building")
    subparsers.add_parser("publish", help="Publish the built archive as this version's release")

    args = parser.parse_args()
    workdir = Path(args.workdir).resolve()
    target = get_target(load_config(workdir / args.config), args.target)

    if args.command == "check":
        command_check(target, workdir)
    else:
        command_publish(target, workdir)


if __name__ == "__main__":
    sys.exit(main())
