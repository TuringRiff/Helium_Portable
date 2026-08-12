"""Publish one GitHub Release per Helium version.

Every new upstream version gets its own release, tagged `v{version}`. Re-running
for a version that already has a release updates it in place (replaces the
archive asset and refreshes the notes), which also covers manual rebuilds.

This script replaces the builder's shared-release machinery (`render-release` /
`update-release`), because Helium's package version differs from the bundled
Chromium version that the single-target builder treats as `{version}`. The
builder's `archive` stage leaves the archive name, digest and size in
`ARCHIVE_*` env vars, which this script reads to render the release notes.
"""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from string import Formatter
from urllib.parse import quote

import requests

PLACEHOLDER_PATTERNS = {
    "version": r"\d+(?:\.\d+)+",
    "package_version": r"\d+(?:\.\d+)+",
    "date": r"\d{4}-\d{2}-\d{2}",
    "arch": r"[A-Za-z0-9][A-Za-z0-9.-]*",
}


def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Helium_Portable publisher",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def human_size(size):
    """Format bytes the way Windows Explorer does (1024-based, labelled)."""
    if size in (None, ""):
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def format_value(template, context):
    return str(template).format(**context)


def build_context(target, version, date):
    return {
        "target": target.get("target", ""),
        "name": target.get("name", target.get("display_name", "")),
        "display_name": target.get("display_name", target.get("name", "")),
        "output_dir": target.get("output_dir", target.get("name", "Browser")),
        "version": version,
        "package_version": version,
        "date": date,
        "arch": target.get("architecture", "x64"),
        "archive": os.getenv("ARCHIVE_NAME", ""),
        "sha256": os.getenv("ARCHIVE_SHA256", ""),
        "size": human_size(os.getenv("ARCHIVE_SIZE")),
        "chrome_plus_version": os.getenv("CHROME_PLUS_VERSION", ""),
        "run_url": build_run_url(),
    }


def build_run_url():
    repo = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if not repo or not run_id:
        return ""
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/actions/runs/{run_id}"


def archive_name_matcher(target):
    """Regex that matches any archive this target could have produced.

    Used to prune stale assets (e.g. an older date suffix) before uploading a
    rebuilt archive into an existing release.
    """
    template = target.get("archive_name", "")
    parts = ["^"]
    for literal_text, field_name, format_spec, conversion in Formatter().parse(template):
        del format_spec, conversion
        parts.append(re.escape(literal_text))
        if field_name is not None:
            parts.append(PLACEHOLDER_PATTERNS.get(field_name, r"[^/]+?"))
    parts.append("$")
    return re.compile("".join(parts), re.IGNORECASE)


def list_releases(repo):
    response = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        params={"per_page": 100},
        headers=github_headers(),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def find_release_by_tag(releases, tag):
    return next((release for release in releases if release.get("tag_name") == tag), None)


def delete_asset(repo, asset_id):
    response = requests.delete(
        f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}",
        headers=github_headers(),
        timeout=60,
    )
    if response.status_code != 204:
        raise RuntimeError(f"Failed to delete asset {asset_id}: {response.status_code} {response.text}")


def upload_asset(repo, release_id, asset_path):
    url = f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={quote(asset_path.name)}"
    with asset_path.open("rb") as file:
        response = requests.post(
            url,
            data=file,
            headers={**github_headers(), "Content-Type": "application/octet-stream"},
            timeout=1800,
        )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Failed to upload {asset_path.name}: {response.status_code} {response.text}")


def find_archive(workdir):
    asset_env = os.getenv("ASSET_PATH")
    if asset_env:
        path = Path(asset_env)
        if not path.is_absolute():
            path = Path(workdir) / path
        if not path.exists():
            raise RuntimeError(f"ASSET_PATH does not exist: {path}")
        return path

    assets_dir = Path(workdir) / "build" / "assets"
    matches = sorted(assets_dir.glob("*.7z"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError(f"No archive found in {assets_dir}.")
    return matches[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="browser.json")
    parser.add_argument("--target", default="helium_stable")
    parser.add_argument("--workdir", default=".")
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY is not set; publish must run inside GitHub Actions.")
    if not os.getenv("GITHUB_TOKEN"):
        raise RuntimeError("GITHUB_TOKEN is not set.")

    config = json.loads((workdir / args.config).read_text(encoding="utf-8"))
    target = config.get("targets", {}).get(args.target)
    if not target:
        raise RuntimeError(f"Target '{args.target}' not found in {args.config}.")

    version = os.getenv("PACKAGE_VERSION") or os.getenv("UPSTREAM_VERSION")
    if not version:
        raise RuntimeError("PACKAGE_VERSION/UPSTREAM_VERSION not set; run the build step first.")
    build_date = os.getenv("BUILD_DATE") or datetime.now().strftime("%Y-%m-%d")

    context = build_context(target, version, build_date)
    release_config = target.get("release", {})
    tag = format_value(release_config.get("tag", "v{version}"), context)
    title = format_value(release_config.get("title", "{display_name} {version}"), context)
    body = format_value(release_config.get("body", ""), context)

    version_pattern = release_config.get("version_pattern")
    if version_pattern and not re.search(version_pattern, body, re.IGNORECASE):
        raise RuntimeError(
            f"version_pattern {version_pattern!r} does not match the rendered body; "
            "`check` would never find the published version again."
        )

    asset_path = find_archive(workdir)
    print(f"[INFO] Publishing tag={tag} title={title}")
    print(f"[INFO] Archive: {asset_path.name} ({asset_path.stat().st_size} bytes)")

    releases = list_releases(repo)
    existing = find_release_by_tag(releases, tag)
    if existing:
        print(f"[INFO] Release {tag} already exists; updating it in place.")
        response = requests.patch(
            f"https://api.github.com/repos/{repo}/releases/{existing['id']}",
            headers=github_headers(),
            json={"name": title, "body": body},
            timeout=60,
        )
        response.raise_for_status()
        matcher = archive_name_matcher(target)
        for asset in existing.get("assets", []):
            if matcher.match(asset.get("name", "")):
                print(f"[INFO] Removing stale asset: {asset['name']}")
                delete_asset(repo, asset["id"])
        release_id = existing["id"]
    else:
        print(f"[INFO] Creating new release {tag}.")
        response = requests.post(
            f"https://api.github.com/repos/{repo}/releases",
            headers=github_headers(),
            json={
                "tag_name": tag,
                "name": title,
                "body": body,
                "prerelease": False,
                "draft": False,
            },
            timeout=60,
        )
        response.raise_for_status()
        release_id = response.json()["id"]

    upload_asset(repo, release_id, asset_path)
    print(f"[INFO] Published {tag} with {asset_path.name}.")


if __name__ == "__main__":
    main()
