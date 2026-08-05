# SubFix 3.0 测试版

适用：Apple Silicon Mac（M1/M2/M3/M4 及后续芯片）与 DaVinci Resolve Studio。

## 安装

1. 解压 `SubFix3.0测试版.zip`。
2. 在 Finder 中右键 `SubFix3.0测试版.pkg`，选择“打开”。
3. 按系统提示完成安装后，重启 DaVinci Resolve。
4. 在 Resolve 的 `Workspace → Scripts → Utility → SubFix` 打开插件；“生成选区字幕”位于同一菜单下的 SubFix 子菜单。

这是未签名测试版。若 macOS 拦截，请到“系统设置 → 隐私与安全性”选择仍要打开。

## 首次使用

- 豆包（云端）：在生成字幕窗口选择“豆包（云端）”，填写 API Key，即可使用；不需要安装本地 Qwen。
- Qwen（本地）：在生成字幕窗口选择“Qwen（本地）”，按提示下载安装识别环境和模型。模型体积较大，首次安装需要网络与充足磁盘空间。
- 规整字幕长度：基础包已包含 Qwen 强制对齐组件，不需要额外下载。

## 卸载

先退出 Resolve，再双击随包提供的“卸载_SubFix”程序，选择完整卸载。

也可以手动删除以下目录：

`~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix`

`~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.subfix_support`
