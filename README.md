# SubFix v3.2.8

DaVinci Resolve 字幕插件。口播、现场、单段和批量字幕生成统一使用 v5。

## 安装

从 [GitHub Releases](https://github.com/HooperH/SubFix/releases/latest) 下载完整安装 ZIP，解压并打开 pkg。支持 Apple Silicon Mac，沿用未签名安装方式。首次使用本地 Qwen 识别仍需联网安装识别依赖和模型。

## 源码与构建

本快照包含插件运行代码和安装包构建脚本。私人字幕、项目样本、开发记录和依赖这些样本的内部测试不在公开快照中。旧副本和平台缓存需要单独处理，不能保证已下载的资料被收回。

完整构建使用 `build_subfix_test_package.sh`，需要设置 `ALIGNER_MODEL` 为 Qwen 强制对齐 GGUF 模型路径，`QWEN_BUILD` 为编译好的 qwen3-asr.cpp 运行时目录，并设置 `VERSION=3.2.8`。脚本内置 Python 与 FFmpeg 下载、校验和打包流程。`build_pkg.sh` 仅生成轻量包。

v5 复用了 `subfix_generate_v4.py` 中的基础算法；文件名不代表仍可选择旧引擎。公开配置不包含原始训练字幕，保留运行所需的模型权重。
