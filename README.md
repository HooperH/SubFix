# SubFix

SubFix is a DaVinci Resolve subtitle workflow script for managing, correcting,
translating, backing up, and restoring subtitle text inside Resolve.

> Latest source note: the canonical latest code in this repository is
> `SubFix.lua` at the project root. It is newer than the packaged
> `dist/SubFix_v2.0_macOS` copy and includes post-2.0 fixes.

## Features

- Refresh and manage timeline subtitle rows.
- Batch subtitle text replacement and cleanup.
- AI-assisted correction and translation workflow.
- Backup and restore workflow for subtitle changes.
- macOS package build script for local distribution.

## Install

Copy `SubFix.lua` to one of DaVinci Resolve's Fusion script folders:

```bash
mkdir -p "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
cp SubFix.lua "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix.lua"
```

Then open DaVinci Resolve and run it from:

```text
Workspace -> Scripts -> SubFix
```

You can also use the helper script:

```bash
./sync_to_plugin.sh
```

## Build macOS Package

The package build script uses the root `SubFix.lua` source file by default:

```bash
VERSION=2026.05.15 ./build_pkg.sh
```

Build output is written to `dist/`. Generated packages and zip files are ignored
by Git so this repository stays source-first.

## Publish to GitHub

After creating an empty GitHub repository, connect and push this local project:

```bash
git remote add origin <your-github-repo-url>
git push -u origin main
```

## Files

- `SubFix.lua` - latest script source.
- `sync_to_plugin.sh` - copies the latest script into the Resolve scripts folder.
- `build_pkg.sh` - builds a macOS `.pkg` and release folder.
- `build_uninstaller.sh` / `卸载_SubFix.command` - uninstaller helpers.
- `CHANGELOG.md` - latest notable changes.

## License

No open-source license has been declared yet. Add a license before public reuse
if you want others to copy, modify, or redistribute the project.
