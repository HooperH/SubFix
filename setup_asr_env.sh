#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_VENV_DIR="$SCRIPT_DIR/.subfix_asr_env"
USER_VENV_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.subfix_support/.subfix_asr_env"
if [[ -n "${SUBFIX_ASR_VENV_DIR:-}" ]]; then
  VENV_DIR="$SUBFIX_ASR_VENV_DIR"
elif [[ -w "$SCRIPT_DIR" ]] || [[ -w "$LOCAL_VENV_DIR" ]]; then
  VENV_DIR="$LOCAL_VENV_DIR"
else
  VENV_DIR="$USER_VENV_DIR"
fi
BUNDLED_PY="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

if [[ -x "$BUNDLED_PY" ]]; then
  PYTHON="$BUNDLED_PY"
else
  PYTHON="$(command -v python3)"
fi

echo "🔧 使用 Python: $PYTHON"
echo "📁 ASR venv: $VENV_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install 'stable-ts[mlx]'
"$VENV_DIR/bin/python" -m pip install 'whisperx'
"$VENV_DIR/bin/python" -m pip install 'torch' 'transformers'

if [[ "${SUBFIX_INSTALL_QWEN_ASR:-1}" != "0" ]]; then
  "$VENV_DIR/bin/python" -m pip install -U 'qwen-asr'
  echo "✅ Qwen3-ASR 已安装。首次生成会下载 Qwen/Qwen3-ASR-1.7B 和 Qwen/Qwen3-ForcedAligner-0.6B。"
else
  echo "ℹ️ 已按 SUBFIX_INSTALL_QWEN_ASR=0 跳过 Qwen3-ASR。v4 生成字幕将不可用。"
fi

echo "✅ ASR 环境已安装。首次规整会下载 CTC/WhisperX/stable-ts 对齐模型。"
