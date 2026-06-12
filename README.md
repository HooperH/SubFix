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

---

# 中文说明

SubFix 是一款面向 DaVinci Resolve 的字幕管理插件，适合需要高频处理字幕的剪辑师和后期团队。它可以帮助你更快地清理、纠错、翻译、复核、备份和恢复时间线里的字幕文本。

它更适合字幕量较大的工作流，例如访谈、课程、短剧、社媒短片，以及需要结合 AI 做字幕后期处理的项目。

> 最新版说明：本仓库的最新版代码是项目根目录下的 `SubFix.lua`。它比已经打包的 `dist/SubFix_v2.0_macOS` 版本更新，并包含 2.0 之后的修复。

## 为什么需要 SubFix

在 DaVinci Resolve 里处理大量字幕时，手动修改会很慢，也容易出错。尤其是几百行字幕的项目里，AI 返回内容可能漏行，批量替换也可能误伤原文；如果没有备份和恢复机制，回滚成本会很高。

SubFix 主要解决这些问题：

- 让时间线字幕行更容易刷新、查看和管理。
- 支持批量查找、替换和清理字幕文本。
- 支持 AI 辅助纠错和翻译。
- 当 AI 少量漏回字幕行时，可自动保留原文，避免整批结果作废。
- 在高风险批量操作前备份字幕内容。
- 当批量修改不符合预期时，可以恢复之前的字幕状态。

## 核心功能

- DaVinci Resolve 内字幕刷新与列表管理。
- 批量查找、替换、清理字幕文本。
- AI 辅助字幕纠错与翻译。
- AI 漏行兜底：缺失行可保留原字幕文本，不必整批失败。
- 字幕修改备份与恢复。
- macOS 安装包构建脚本。
- 面向 DaVinci Resolve 20.x 的工作流。

## 当前版本

本 GitHub 仓库跟踪的是 2026-05-15 的最新工作版 `SubFix.lua`，不是旧的 `dist/SubFix_v2.0_macOS` 打包版本。

2.0 之后的主要修复包括：

- 兼容部分不再接受 `temperature` 参数的新 AI 模型。
- 当 AI 批量任务少量漏回行时，自动保留原文，降低整批失败概率。

## 安装

将 `SubFix.lua` 复制到 DaVinci Resolve 的 Fusion 脚本目录：

```bash
mkdir -p "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
cp SubFix.lua "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix.lua"
```

然后在 DaVinci Resolve 中运行：

```text
Workspace -> Scripts -> SubFix
```

也可以使用同步脚本：

```bash
./sync_to_plugin.sh
```

## 构建 macOS 安装包

打包脚本默认使用根目录下的最新版 `SubFix.lua`：

```bash
VERSION=2026.05.15 ./build_pkg.sh
```

构建产物会输出到 `dist/`。安装包和 zip 分发文件已被 Git 忽略，因此仓库保持源码优先。

## 文件说明

- `SubFix.lua` - 最新插件源码。
- `sync_to_plugin.sh` - 将最新版脚本同步到 DaVinci Resolve 脚本目录。
- `build_pkg.sh` - 构建 macOS `.pkg` 安装包和分发目录。
- `build_uninstaller.sh` / `卸载_SubFix.command` - 卸载辅助脚本。
- `CHANGELOG.md` - 版本变化说明。

## 许可

当前还没有声明开源许可证。如果希望其他人复制、修改或再分发本项目，建议先补充明确的许可证。
