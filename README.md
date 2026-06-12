# SubFix

SubFix is a DaVinci Resolve subtitle management plugin for editors who need to
clean, correct, translate, review, back up, and restore subtitle text faster.

It is designed for subtitle-heavy workflows such as interviews, courses, short
dramas, social clips, and AI-assisted post-production.

> Latest source note: the canonical latest code in this repository is
> `SubFix.lua` at the project root. It is newer than the packaged
> `dist/SubFix_v2.0_macOS` copy and includes post-2.0 fixes.

## Why SubFix

Subtitle work in DaVinci Resolve can become slow and fragile when a project has
hundreds of lines. Manual correction is repetitive, AI output can miss lines,
and large batch edits are risky without a recovery path.

SubFix focuses on making that workflow safer:

- Keep timeline subtitle rows visible and manageable.
- Apply batch text cleanup without losing track of changes.
- Use AI for correction and translation while keeping guardrails around output.
- Preserve original text when AI misses returned lines.
- Back up subtitle edits before applying risky operations.
- Restore previous subtitle states when a batch change needs to be rolled back.

## Core Features

- Subtitle refresh and list management inside DaVinci Resolve.
- Batch find, replace, and cleanup workflows.
- AI-assisted subtitle correction and translation.
- Missing-line fallback for AI responses: unchanged lines can keep their
  original subtitle text instead of failing the whole batch.
- Backup and restore workflow for subtitle changes.
- macOS package build script for local distribution.
- DaVinci Resolve 20.x oriented workflow.

## Current Version

This GitHub repository tracks the latest working `SubFix.lua` source from
2026-05-15, not the older packaged `dist/SubFix_v2.0_macOS` copy.

Notable post-2.0 fixes include:

- Newer AI model compatibility for requests where `temperature` is rejected.
- Safer AI batch handling when a model omits a small number of returned lines.

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

## Files

- `SubFix.lua` - latest script source.
- `sync_to_plugin.sh` - copies the latest script into the Resolve scripts folder.
- `build_pkg.sh` - builds a macOS `.pkg` and release folder.
- `build_uninstaller.sh` / `卸载_SubFix.command` - uninstaller helpers.
- `CHANGELOG.md` - latest notable changes.

## License

No open-source license has been declared yet. Add a license before public reuse
if you want others to copy, modify, or redistribute the project.
