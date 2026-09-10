# SubFix v3.2.10

DaVinci Resolve 字幕插件。口播、现场、单段和批量字幕生成统一使用 v5。

## 安装

从 [GitHub Releases](https://github.com/HooperH/SubFix/releases/latest) 下载完整安装 ZIP，解压并打开 pkg。支持 Apple Silicon Mac，沿用未签名安装方式。首次使用本地 Qwen 识别仍需联网安装识别依赖和模型。

## 源码与构建

本快照包含插件运行代码和安装包构建脚本。私人字幕、项目样本、开发记录和依赖这些样本的内部测试不在公开快照中。旧副本和平台缓存需要单独处理，不能保证已下载的资料被收回。

完整构建使用 `build_subfix_test_package.sh`，需要设置 `ALIGNER_MODEL` 为 Qwen 强制对齐 GGUF 模型路径，`QWEN_BUILD` 为编译好的 qwen3-asr.cpp 运行时目录，并设置 `VERSION=3.2.10`。脚本内置 Python 与 FFmpeg 下载、校验和打包流程。`build_pkg.sh` 仅生成轻量包。

v5 复用了 `subfix_generate_v4.py` 中的基础算法；文件名不代表仍可选择旧引擎。公开配置不包含原始训练字幕，保留运行所需的模型权重。

## AI 工作台

任务统一为完整纠错、的地得专项检测、中英翻译和简繁转换，使用纯文字菜单。翻译和简繁转换根据字幕抽样自动判断方向，再对整批字幕应用同一方向；依据不足或检测失败时停止并保留原字幕。简繁转换只处理字形，不替换地区用语，不改变字幕时间。

## 本地 Qwen 下载

首次安装依赖使用清华 PyPI 镜像；识别模型优先从魔搭 ModelScope 下载，失败后尝试 Hugging Face 备用源。下载源显示在进度中，保留已下载文件。国内源的可用性仍取决于当前网络。已有完整环境和模型继续复用。镜像配置仅作用于 SubFix 安装命令，不修改系统 pip 或代理配置。

[Qwen 官方模型下载说明](https://github.com/QwenLM/Qwen3-ASR#released-models-description-and-download)推荐中国大陆用户使用 ModelScope。
