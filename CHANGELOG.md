# Changelog

## 3.1.7 - 2026-08-17

- 新增“生成选区字幕”热词库：可管理常用品牌、产品和专名，并在 Qwen 转写与时间对齐后进行安全标准化。
- 增加热词库一键清空确认、连续新增输入清空，以及生成窗口热词开关。
- 修复豆包转写后使用本地 Qwen 强制对齐的运行环境选择与进度显示。

## 3.1.6 - 2026-08-13

- 加快 SubFix 主窗口首次打开速度：完整界面按需初始化。
- 修复连续编辑字幕时弹窗叠加、无法关闭的问题。
- 在 AI 参考文稿输入区增加“清空”按钮，一键清空参考文本。
- 加快“生成选区字幕”脚本启动：不再扫描整条视频轨来计算可选的匹配画面标记。
- 保留 3.1.5 的内置 FFmpeg 更新与首次安装支持。

## Latest working copy - 2026-07-03

This is the version intended for the GitHub sharing project. It uses the root
`SubFix.lua`, not the packaged `dist/SubFix_v2.0_macOS/SubFix.lua` copy.

Changes after the 2026-05-15 working copy:

- Add a separate `SubFix_GenerateSelectionSubtitles.lua` DaVinci child plugin
  for generating subtitles from the current In/Out selection.
- Generate selection subtitles from source audio clips directly, including
  audio track selection, linked-channel detection, progress UI, cancellation,
  and selected-range SRT writeback.
- Rebuild the target subtitle track from generated rows while preserving
  subtitles outside the selection and restoring the original playhead.
- Add `generate_subtitles` mode to `subfix_asr_transcribe.py`, including
  readable Chinese/mixed-language subtitle splitting, weighted timing
  distribution, `subtitle_rows` output, and optional SRT export.
- Update `sync_to_plugin.sh` to install the child plugin alongside `SubFix.lua`
  and the ASR helper files.
- Allow pre-delivery timeline-audio speech checks to override export padding
  through `options.padding_seconds`.
- Improve child-plugin In/Out frame normalization for one-hour timeline starts
  and log raw-to-normalized selection diagnostics.
- Improve generated-subtitle audio source selection by using
  `GetSourceStartFrame()`, surfacing linked-channel metadata, deprioritizing
  muted mappings, and writing a selected-audio-source diagnostic JSON file.
- Keep generated-subtitle writeback on the stable SRT append path without
  pre-moving the playhead to the timeline start.

## Latest working copy - 2026-05-15

This is the version intended for the GitHub sharing project. It uses the root
`SubFix.lua`, not the packaged `dist/SubFix_v2.0_macOS/SubFix.lua` copy.

Changes after the packaged 2.0 copy:

- Skip the `temperature` request field for newer model families that reject it,
  including Claude 4/5 style names, OpenAI o-series names, and GPT-5 style names.
- For single-pass AI translation or correction tasks, keep original subtitle text
  for missing AI response lines instead of discarding the whole batch.

## 2.0 package - 2026-03-18

- Native TabBar and Stack UI architecture.
- Timecode and update engine.
- Backup and restore workflow.
- AI correction workflow.
