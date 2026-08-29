# Changelog

## 3.2.3 - 2026-08-29

- 修复选择豆包极速版或录音文件识别 2.0 后，空结果恢复、局部重试或时间轴对齐仍要求安装 Qwen 的问题。
- 豆包识别全流程保持使用所选云端后端；优先使用词级时间戳，其次使用句级时间戳，缺失时在本地完成确定性时间轴映射。
- 豆包极速版请求开启句级时间戳返回，提升缺失词级时间戳时的字幕定位质量。

## 3.2.2 - 2026-08-29

- 修复 v3.2.0 用户通过“检查更新”安装 v3.2.1 时提示“更新包包含未授权文件”的问题。
- 增量更新包保持兼容既有安全白名单；完整安装包继续包含独立后台进程管理器。
- 发布流程新增旧版更新器兼容验收，避免新增文件再次阻断存量用户更新。

## 3.2.1 - 2026-08-29

- 豆包云端识别支持在配置面板中选择极速版或录音文件识别 2.0 标准版，并记住上次选择。
- 改进长任务进度：显示有效音频时长和解析、合并、备份、写回阶段；长时间无新进度时给出可取消提示。
- 改进取消安全性：后台识别使用独立进程组，字幕备份分块响应取消，写回开始后锁定取消入口。
- 优化 v5 多音轨生成性能、热词上下文与中英文边界处理，并将写回 JSON 精简为字幕必需字段。
- 加固本地 Qwen 安装与完整安装包权限、运行时路径和用户级部署流程。

## 3.2.0 - 2026-08-18

- 修复完整安装包 postinstall 在成功清理旧系统级 SubFix 后仍向 macOS 安装器返回失败状态的问题。
- 完整安装和检查更新均只保留当前用户目录下的 SubFix 菜单入口，避免 Resolve 出现重复插件。

## 3.1.9 - 2026-08-18

- 修复 3.1.8 安装后脚本在系统级旧入口已清理时错误返回失败状态的问题。

## 3.1.8 - 2026-08-17

- 修复完整安装包同时注册系统级与用户级 SubFix 导致 Resolve 菜单出现两个插件的问题。
- 安装和检查更新均以用户级脚本目录为唯一入口，并在成功写入后清理旧系统级菜单入口。

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
