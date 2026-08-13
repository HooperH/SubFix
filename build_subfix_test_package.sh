#!/bin/bash
# Build the standalone, unsigned Apple-Silicon SubFix beta package.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="${VERSION:-3.0-beta}"
RELEASE_NAME="${RELEASE_NAME:-SubFix3.0测试版}"
BUILD_ROOT="${BUILD_ROOT:-$SCRIPT_DIR/build/subfix-test-package}"
RELEASE_ROOT="${RELEASE_ROOT:-$SCRIPT_DIR/dist}"
RELEASE_DIR="$RELEASE_ROOT/$RELEASE_NAME"
PAYLOAD_DIR="$BUILD_ROOT/payload"
PKG_SCRIPTS_DIR="$BUILD_ROOT/pkg-scripts"
UNINSTALLER_BUILD_DIR="$BUILD_ROOT/uninstaller"
RUNTIME_DIR="$BUILD_ROOT/runtime/python"
QWEN_BIN_DIR="$BUILD_ROOT/qwen-bin"
PKG_PATH="$RELEASE_DIR/SubFix-v${VERSION}-macOS.pkg"
ZIP_PATH="$RELEASE_ROOT/$RELEASE_NAME.zip"
INSTALL_PATH="Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SUPPORT_PAYLOAD="$PAYLOAD_DIR/$INSTALL_PATH/.subfix_support"

PYTHON_RUNTIME_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260718/cpython-3.12.13%2B20260718-aarch64-apple-darwin-install_only.tar.gz"
PYTHON_RUNTIME_SHA256="62aeee6161d57303a71a138b75fd5cc6fb8c89c4b1d9c7f0a052d89fa0b6652b"
ALIGNER_MODEL="$SCRIPT_DIR/.subfix_support/models/qwen3-forced-aligner-0.6b-f16.gguf"
QWEN_BUILD="$SCRIPT_DIR/.subfix_support/qwen3-asr.cpp/build"
USER_INSTALL_SCRIPT="$SCRIPT_DIR/scripts/install_subfix_for_user.sh"

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "❌ 当前版本仅支持 Apple Silicon Mac（arm64）。" >&2
  exit 1
fi

for required in \
  "$SCRIPT_DIR/SubFix.lua" \
  "$SCRIPT_DIR/生成选区字幕.lua" \
  "$SCRIPT_DIR/.subfix_support/subfix_generate_selection_core.lua" \
  "$SCRIPT_DIR/.subfix_support/subfix_update.py" \
  "$SCRIPT_DIR/.subfix_support/subfix_qwen_local_manager.py" \
  "$SCRIPT_DIR/subfix_asr_transcribe.py" \
  "$SCRIPT_DIR/subfix_generate_v4.py" \
  "$SCRIPT_DIR/subfix_generate_v5.py" \
  "$SCRIPT_DIR/subfix_generate_textnorm.py" \
  "$SCRIPT_DIR/setup_asr_env.sh" \
  "$USER_INSTALL_SCRIPT" \
  "$ALIGNER_MODEL" \
  "$QWEN_BUILD/qwen3-asr-cli"; do
  [[ -f "$required" ]] || { echo "❌ 缺少发行文件：$required" >&2; exit 1; }
done

mkdir -p "$BUILD_ROOT" "$RELEASE_ROOT"
if [[ ! -x "$RUNTIME_DIR/bin/python3" ]]; then
  echo "⬇️ 下载并校验内置 Python runtime…"
  PYTHON_RUNTIME_URL="$PYTHON_RUNTIME_URL" PYTHON_RUNTIME_SHA256="$PYTHON_RUNTIME_SHA256" \
    "$SCRIPT_DIR/scripts/fetch_python_runtime.sh" "$RUNTIME_DIR"
fi
"$RUNTIME_DIR/bin/python3" -c 'import platform,sys; assert platform.machine()=="arm64"; assert sys.version_info[:2]==(3,12)'

echo "📦 暂存 Qwen 强制对齐运行时…"
"$SCRIPT_DIR/scripts/stage_qwen3_cpp_runtime.sh" "$QWEN_BUILD" "$QWEN_BIN_DIR"

rm -rf "$PAYLOAD_DIR" "$PKG_SCRIPTS_DIR" "$UNINSTALLER_BUILD_DIR" "$RELEASE_DIR"
rm -f "$ZIP_PATH"
mkdir -p "$SUPPORT_PAYLOAD" "$PAYLOAD_DIR/$INSTALL_PATH/SubFix" "$PKG_SCRIPTS_DIR" "$RELEASE_DIR"

cp "$SCRIPT_DIR/SubFix.lua" "$PAYLOAD_DIR/$INSTALL_PATH/SubFix/SubFix.lua"
cp "$SCRIPT_DIR/生成选区字幕.lua" "$PAYLOAD_DIR/$INSTALL_PATH/SubFix/生成选区字幕.lua"
for helper in subfix_generate_selection_core.lua subfix_update.py subfix_qwen_local_manager.py segmentation_profile.json segmentation_profile_v3.json segmentation_profile_v4.json; do
  cp "$SCRIPT_DIR/.subfix_support/$helper" "$SUPPORT_PAYLOAD/$helper"
done
for helper in subfix_asr_transcribe.py subfix_generate_v4.py subfix_generate_v5.py subfix_generate_textnorm.py setup_asr_env.sh; do
  cp "$SCRIPT_DIR/$helper" "$SUPPORT_PAYLOAD/$helper"
done
mkdir -p "$SUPPORT_PAYLOAD/runtime"
cp -R "$RUNTIME_DIR" "$SUPPORT_PAYLOAD/runtime/python"
cp -R "$QWEN_BIN_DIR" "$SUPPORT_PAYLOAD/bin"
mkdir -p "$SUPPORT_PAYLOAD/models"
cp "$ALIGNER_MODEL" "$SUPPORT_PAYLOAD/models/qwen3-forced-aligner-0.6b-f16.gguf"
chmod 755 "$PAYLOAD_DIR/$INSTALL_PATH/SubFix/SubFix.lua" "$PAYLOAD_DIR/$INSTALL_PATH/SubFix/生成选区字幕.lua" \
  "$SUPPORT_PAYLOAD/setup_asr_env.sh" "$SUPPORT_PAYLOAD/subfix_asr_transcribe.py" "$SUPPORT_PAYLOAD/subfix_qwen_local_manager.py" "$SUPPORT_PAYLOAD/bin/qwen3-asr-cli"

cat > "$PKG_SCRIPTS_DIR/preinstall" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "当前版本仅支持 Apple Silicon Mac（M1/M2/M3/M4 及后续芯片），不支持 Intel Mac。" >&2
  exit 1
fi
EOF
chmod 755 "$PKG_SCRIPTS_DIR/preinstall"
cp "$USER_INSTALL_SCRIPT" "$PKG_SCRIPTS_DIR/postinstall"
chmod 755 "$PKG_SCRIPTS_DIR/postinstall"

pkgbuild --root "$PAYLOAD_DIR" --scripts "$PKG_SCRIPTS_DIR" --identifier com.mediastorm.subfix.beta \
  --version "$VERSION" --install-location / --quiet "$PKG_PATH"

cp "$SCRIPT_DIR/README-SubFix3.0测试版.md" "$RELEASE_DIR/安装说明.md"
OUTPUT_DIR="$UNINSTALLER_BUILD_DIR" APP_NAME="卸载_SubFix.app" COMMAND_NAME="卸载_SubFix.command" \
  "$SCRIPT_DIR/build_uninstaller.sh"
if [[ -d "$UNINSTALLER_BUILD_DIR/卸载_SubFix.app" ]]; then
  cp -R "$UNINSTALLER_BUILD_DIR/卸载_SubFix.app" "$RELEASE_DIR/"
elif [[ -f "$UNINSTALLER_BUILD_DIR/卸载_SubFix.command" ]]; then
  cp "$UNINSTALLER_BUILD_DIR/卸载_SubFix.command" "$RELEASE_DIR/"
else
  echo "❌ 未能生成 SubFix 卸载程序" >&2
  exit 1
fi
ditto -c -k --sequesterRsrc --keepParent "$RELEASE_DIR" "$ZIP_PATH"

echo "✅ 已生成：$ZIP_PATH"
echo "ℹ️ 未签名测试版：首次安装请在 Finder 右键安装包并选择“打开”。"
