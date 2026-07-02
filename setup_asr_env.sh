#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.subfix_asr_env"
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

if [[ "${SUBFIX_INSTALL_QWEN_ASR:-0}" == "1" ]]; then
  "$VENV_DIR/bin/python" -m pip install -U 'qwen-asr'
  echo "✅ Qwen3-ASR 可选依赖已安装。首次终检会下载 Qwen/Qwen3-ASR-1.7B 和 Qwen/Qwen3-ForcedAligner-0.6B。"
else
  echo "ℹ️ 跳过 Qwen3-ASR 可选依赖。如需启用：SUBFIX_INSTALL_QWEN_ASR=1 ./setup_asr_env.sh"
fi

echo "✅ ASR 环境已安装。首次规整会下载 CTC/WhisperX/stable-ts 对齐模型。"
