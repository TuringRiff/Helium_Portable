# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Helium Portable — automated portable build of the [Helium](https://github.com/imputnet/helium) browser for Windows x64, bundled with Chrome++ portability features. The build pipeline runs on GitHub Actions, tracks the latest stable release from `imputnet/helium-windows`, and publishes each new version as its own GitHub Release tagged `v{version}`.

## Local Build

```powershell
python -m pip install requests
$env:PYTHONPATH="..\ChromiumPortable"
$env:HELIUM_EXTRACT_INNER="true"
python -m portable_builder --config browser.json --target helium_stable --workdir . build
python -m portable_builder --config browser.json --target helium_stable --workdir . archive
python -m portable_builder --config browser.json --target helium_stable --workdir . verify
```

The `HELIUM_EXTRACT_INNER=true` env var is required — it triggers `helium_package.py` to download the upstream Helium zip, restructure it into a builder archive matching the expected `Helium-bin` version_root layout, and pass the local archive path to the builder instead of a direct download URL.

Other commands (all need `$env:PYTHONPATH`; the release ones also need `$env:GITHUB_TOKEN`):

```powershell
# Check whether the upstream version already has a release
python scripts/release.py --config browser.json --target helium_stable --workdir . check

# Verify the finished archive (extract, check the import table, launch the browser)
python -m portable_builder --config browser.json --target helium_stable --workdir . verify
```

`verify` needs no `HELIUM_EXTRACT_INNER`.

## Releases

One release per Helium version, tagged `v{package_version}`, handled end to end by `scripts/release.py`. The builder's `check`, `render-release` and `update-release` commands are all deliberately unused: Helium's package version is not the bundled Chromium version that the single-target builder treats as `{version}`, so tag, title and archive name must come from `{package_version}`.

`check` decides by asking GitHub for the release on tag `v{package_version}` — no release, or a release carrying no matching archive, means build. The tag is therefore the single record of what has shipped, which is what lets release notes be reworded freely and still detects a lower-numbered upstream hotfix. Do not switch back to the builder's `check`: it recovers the published version by regex-matching the newest release's Markdown body, a leftover of the old shared-release model that silently rebuilds forever once the wording and the pattern drift apart.

`publish` creates the release as a draft, uploads the archive, then flips it public, so a tag never points at a release users can see but cannot download from. Re-running for a version that already has a release updates it in place, which covers manual rebuilds.

## Key Files

- **`browser.json`** — Build target config. Defines the single `helium_stable` target plus its per-target `release` section (tag, title, body). Placeholders mean the same thing everywhere in this file: `{package_version}` is the Helium release version (`0.15.4.1`) and `{version}` is the bundled Chromium version directory name (`140.0.x.y`). Tag, title and `archive_name` therefore all use `{package_version}`; `{version}` appears only where the Chromium build is genuinely what is meant.
- **`scripts/helium_package.py`** — The script provider. Queries `imputnet/helium-windows` releases, selects the latest stable release, finds the x64 zip asset, and either returns its URL or (with `--extract-inner` / `HELIUM_EXTRACT_INNER`) restructures the zip into a builder-compatible 7z archive. Key behavior: `chrome.exe` goes to `Helium-bin/chrome.exe`, everything else to `Helium-bin/<chromium_version>/...`. It refuses to emit a result unless the GitHub release asset carries a `digest`, verifies the downloaded zip against it, and reports a `sha256` for whatever it hands the builder (the repacked archive in extract-inner mode, the upstream digest otherwise).
- **`scripts/release.py`** — Both halves of the release flow (`check` and `publish`), sharing one `render_tag`, so the two can never disagree about which tag a version belongs to. The tag template is restricted to fields known before the build; allowing `{date}` or `{version}` there would make a rebuild of the same Helium version land on a second tag. `publish` reads the `ARCHIVE_*` / `PACKAGE_VERSION` / `BUILT_VERSION` env vars left by the builder's `build` and `archive` stages, and reuses the builder's `build_context`, `human_size` and `archive_name_regex` rather than reimplementing them — hence `PYTHONPATH` on the publish step too.
- **`chrome++/chrome++.override.ini`** — Only this browser's deviations from the shared baseline. Its command line disables the profile lock, points Helium's browser-only WinSparkle appcast at the reserved `updates.invalid` domain, and disables the default-browser check. The appcast override is required because upstream's updater silently launches a per-user installer under `%LOCALAPPDATA%\imput\Helium\Application`; it does not disable Chromium component updates. The builder merges core's `setdll/chrome++.ini` (upstream baseline) → `setdll/chrome++.defaults.ini` (project-wide defaults) → this file. The effective `data_dir` is `%app%\..\Data` from the baseline: `%app%` is chrome.exe's directory (`Helium/`), so the profile lands one level up, next to the `Helium` folder in the extracted archive. See *chrome++.ini layering* in the workspace `CLAUDE.md`.
- **`chrome++/injectpe.bat`** — Manual DLL injection helper, unused by the automated build (which calls `setdll` directly). It still targets `helium.exe`, a name upstream no longer ships — the executable is `chrome.exe`.
- **`开始.bat`** — Creates a desktop shortcut pointing to `Helium\chrome.exe`, with the WinSparkle appcast and default-browser protections repeated as defense in depth. Do not restore the old broad `--disable-background-networking` switch: it does not stop WinSparkle and may also suppress extension and component updates.
- **`清理Helium安装版注册表.bat`** — Self-contained recovery tool for removing stale installed-Helium Default Apps registrations and resetting only associations that currently point to Helium. Its ASCII PowerShell payload is embedded after a marker and executed directly from memory, so users only need this one file. It backs up touched keys, supports `--dry-run`, never registers the portable build, and leaves browser files/data alone. Keep the entire file ASCII-only with CRLF line endings because localized `cmd.exe` versions may corrupt UTF-8 batch text. The current builder only copies `start_script`, so this recovery tool is not automatically included in release archives.

## Helium-Specific Build Details

- The upstream zip from `imputnet/helium-windows` contains a single root directory with `chrome.exe` at root and versioned files beside it. `helium_package.py` restructures this into `Helium-bin/chrome.exe` + `Helium-bin/<version>/...` to match the builder's expected `version_root` layout.
- The builder's `inject_dll` stage uses `setdll` to inject `version.dll` into `chrome.exe` with a portable relative path. Since `version_dll_location` is `app_root`, the DLL lands next to chrome.exe (not in the version subdirectory). Injection is asserted by parsing the PE import table and requiring `version.dll` to be the *first* import — Chromium natively imports the system `VERSION.dll`, so a weaker check would pass even with no injection at all.
- `exe_name` is `..\\chrome.exe` (relative path from the version subdirectory back to the app root).
- Release tags/archive filenames use the Helium package version (e.g. `v0.15.4.1`, `Helium_0.15.4.1_2026-08-13.7z`), while the internal `Helium-bin/<version>` directory follows the bundled Chromium version required by the upstream layout.
- Each new version gets a fresh release; the publish script never renames or merges an old release. The existing `v0.15.4.1` release predates this model and still carries both stable and preview assets; it is left as-is for history. `check` accepts it as published because its tag matches and it holds an archive matching `archive_name`.
- Re-publishing an older version does not steal the `latest` badge: `publish` compares against `/releases/latest` first and only sets `make_latest` when the version being published is not behind it.
