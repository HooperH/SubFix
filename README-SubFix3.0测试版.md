# SubFix 3.4.0 安装说明

1. 退出 DaVinci Resolve。
2. 打开 `SubFix-v3.4.0-macOS.pkg`，按提示完成安装。
3. 重启 Resolve，从 `Workspace → Scripts → Utility → SubFix` 打开插件。

旧版用户直接覆盖安装，无需卸载，已有设置和模型保留。3.3.9 及更早版本不能通过插件内更新安装 3.4.0，请使用完整 PKG。

适用于 Apple Silicon Mac，安装包未签名。如系统阻止打开，请在 Finder 右键打开并按 macOS 提示处理；确认文件来自 SubFix 官网。

## 模型

基础安装包不包含任何模型权重。完整版右上角“模型中心”提供 Qwen3-ASR-0.6B、Qwen3-ASR-1.7B 和共享对齐模型。识别模型支持在线下载和离线导入；已有模型可继续使用。

豆包云端不需要本地识别模型，精确时间对齐仍需配套共享对齐模型。本地 Qwen 还需准备识别环境。纯空隙规整不需要模型。

官网：https://subfix-by-hooper.ysjf.chatgpt.site/
项目：https://github.com/HooperH/SubFix

共享对齐模型已独立发布，可在模型中心下载，也可从 [模型下载页](https://github.com/HooperH/SubFix/releases/tag/models-v1) 下载后离线导入。模型文件约 1.84 GB，暂由 GitHub 托管，下载速度受网络环境影响；官网安装包不经过 GitHub。
