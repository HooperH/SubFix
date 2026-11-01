# SubFix v3.3.4

DaVinci Resolve 字幕插件。口播、现场、单段和批量字幕生成统一使用 v5。

## 安装

从 [GitHub Releases](https://github.com/HooperH/SubFix/releases/latest) 下载完整安装 ZIP，解压并打开 pkg。支持 Apple Silicon Mac，沿用未签名安装方式。首次使用本地 Qwen 识别仍需联网安装识别依赖和模型。

## 源码与构建

本快照包含插件运行代码和安装包构建脚本。私人字幕、项目样本、开发记录和依赖这些样本的内部测试不在公开快照中。旧副本和平台缓存需要单独处理，不能保证已下载的资料被收回。

完整构建使用 `build_subfix_test_package.sh`，需要设置 `ALIGNER_MODEL` 为 Qwen 强制对齐 GGUF 模型路径，`QWEN_BUILD` 为编译好的 qwen3-asr.cpp 运行时目录，并设置 `VERSION=3.3.4`。脚本内置 Python 与 FFmpeg 下载、校验和打包流程。 `build_pkg.sh` 仅生成轻量包。

v5 复用了 `subfix_generate_v4.py` 中的基础算法；文件名不代表仍可选择旧引擎。公开配置不包含原始训练字幕，保留运行所需的模型权重。

## AI 工作台

任务统一为完整纠错、的地得专项检测、中英翻译和简繁转换，使用纯文字菜单。翻译和简繁转换根据字幕抽样自动判断方向，再对整批字幕应用同一方向；依据不足或检测失败时停止并保留原字幕。简繁转换只处理字形，不替换地区用语，不改变字幕时间。

## 本地 Qwen 下载

首次安装依赖使用清华 PyPI 镜像；识别模型优先从魔搭 ModelScope 下载，失败后尝试 Hugging Face 备用源。下载源显示在进度中，保留已下载文件。国内源的可用性仍取决于当前网络。已有完整环境和模型继续复用。镜像配置仅作用于 SubFix 安装命令，不修改系统 pip 或代理配置。

[Qwen 官方模型下载说明](https://github.com/QwenLM/Qwen3-ASR#released-models-description-and-download)推荐中国大陆用户使用 ModelScope。

## 豆包标准版中断恢复

豆包标准版会逐窗口保存识别结果和未完成任务 ID。遇到 HTTP 504 等临时错误时，先查询原任务，连续查询错误最多重试到 5 次；再次生成相同音频时会复用已完成结果，并继续查询未完成任务。音频内容、识别配置或 API Key 改变时使用独立缓存。

macOS 缓存位置为 `~/Library/Caches/SubFix/doubao_asr_v2`，其他平台为 `~/.cache/SubFix/doubao_asr_v2`；可用 `SUBFIX_DOUBAO_CACHE_DIR` 指定目录。缓存包含转写文本、时间戳和任务 ID，不保存音频或密钥；macOS 文件权限为 `0600`，仅当前用户可读写。缓存会保留以供续跑，清理前请确认不再需要恢复未完成任务。

若服务端持续无法确认原任务状态，插件保留断点并报出请求 ID，不自动换 ID 重提；可据此联系服务商核查。磁盘空间不足或缓存损坏会明确报错。修复前未保存的识别结果无法追溯恢复；续跑只复用识别阶段，后续字幕校验、对齐与分句仍会重新执行。
