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
BUNDLED_PY="$SCRIPT_DIR/runtime/python/bin/python3"

if [[ ! -x "$BUNDLED_PY" ]]; then
  echo "❌ SubFix 内置 Python 缺失，请重新安装完整 SubFix 测试版。" >&2
  exit 1
fi
PYTHON="$BUNDLED_PY"

echo "🔧 使用 Python: $PYTHON"
echo "📁 ASR venv: $VENV_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip

if [[ "${SUBFIX_INSTALL_QWEN_ASR:-1}" != "0" ]]; then
  "$VENV_DIR/bin/python" -m pip install -U 'qwen-asr'
  echo "✅ Qwen3-ASR 已安装。首次生成会下载 Qwen/Qwen3-ASR-1.7B；字幕规整使用内置 Qwen3 Forced Aligner。"
else
  echo "ℹ️ 已按 SUBFIX_INSTALL_QWEN_ASR=0 跳过 Qwen3-ASR。v4 生成字幕将不可用。"
fi

echo "✅ ASR 环境已安装。"
