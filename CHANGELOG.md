# Changelog

## 3.2.5 - 2026-09-09

- 检查更新和下载安装接入统一进度窗口，显示运行状态和耗时，并防止重复触发更新流程。
- 规整字幕长度拆分为可独立选择的“字幕音频对齐”和“消除字幕空隙”，使用紧凑布局和等宽操作按钮。
- 批量替换的查找输入可直接筛选字幕预览，与搜索字幕输入框保持独立。
- 插件窗口按 DaVinci Resolve 主窗口所在显示器居中，修复多屏环境下落到错误屏幕的问题。
- 最终交付检查仅提示超过 3 帧且不超过 2 秒的字幕间隙，与消除字幕空隙的范围一致，避免长时间无对白段落被误报；此项仍为时长规则，不进行声音检测。
- 补齐安装包中字幕文本标准化和本地 Qwen 管理器的依赖文件。

## 3.2.4 - 2026-08-31

- 修复豆包录音文件识别 2.0 在任务仍处于处理或排队状态时，因响应包含空 `result` 对象而被误判为识别完成的问题。
- 豆包 2.0 现在会持续轮询到成功、静音或明确失败状态，避免长任务生成 0 条字幕并触发大量无效重试。
- 容许个别豆包窗口确实没有识别文本时继续处理其他有效窗口，不再因此中断整批字幕写回。

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
