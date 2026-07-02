#!/bin/bash

# =============================================================================
# SubFix - DaVinci Resolve 插件打包脚本
# 功能：生成安装包，并把源码、安装程序、删除程序整理成可分发目录和 ZIP
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_NAME="${PKG_NAME:-SubFix}"
IDENTIFIER="${IDENTIFIER:-com.mediastorm.subfix}"
SOURCE_FILE="${SOURCE_FILE:-${SCRIPT_DIR}/SubFix.lua}"
INSTALL_PATH="Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
BUILD_ROOT="${BUILD_ROOT:-${SCRIPT_DIR}/build}"
PAYLOAD_DIR="${BUILD_ROOT}/payload"
UNINSTALLER_BUILD_DIR="${BUILD_ROOT}/uninstaller"
RELEASE_ROOT="${RELEASE_ROOT:-${SCRIPT_DIR}/dist}"
UNINSTALLER_SCRIPT="${SCRIPT_DIR}/build_uninstaller.sh"
UNINSTALLER_APP_NAME="${UNINSTALLER_APP_NAME:-卸载_SubFix.app}"
UNINSTALLER_COMMAND_NAME="${UNINSTALLER_COMMAND_NAME:-卸载_SubFix.command}"

# 可选签名 / 公证参数
SIGN_IDENTITY="${SIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"

VERSION="${VERSION:-1.0}"
RELEASE_NAME="${PKG_NAME}_v${VERSION}_macOS"
OUTPUT_PKG_NAME="${RELEASE_NAME}.pkg"
OUTPUT_ZIP_NAME="${RELEASE_NAME}.zip"
SIGNED_PKG_NAME="${RELEASE_NAME}_signed.pkg"
RELEASE_DIR="${RELEASE_ROOT}/${RELEASE_NAME}"
OUTPUT_PKG_PATH="${RELEASE_DIR}/${OUTPUT_PKG_NAME}"
OUTPUT_ZIP_PATH="${RELEASE_ROOT}/${OUTPUT_ZIP_NAME}"

echo "📦 开始构建 ${PKG_NAME} v${VERSION} 安装包..."

if [ ! -f "$SOURCE_FILE" ]; then
    echo "❌ 错误：源文件不存在：$SOURCE_FILE"
    exit 1
fi

echo "🧹 清理旧的构建与发布目录..."
rm -rf "$BUILD_ROOT" "$RELEASE_DIR"
rm -f "$OUTPUT_ZIP_PATH"
mkdir -p "$PAYLOAD_DIR/${INSTALL_PATH}" "$RELEASE_DIR"

echo "📄 拷贝源文件到安装载荷目录..."
cp "$SOURCE_FILE" "$PAYLOAD_DIR/${INSTALL_PATH}/SubFix.lua"
chmod 755 "$PAYLOAD_DIR/${INSTALL_PATH}/SubFix.lua"

echo "🗑️ 构建卸载程序..."
if [ ! -x "$UNINSTALLER_SCRIPT" ]; then
    echo "❌ 错误：卸载程序构建脚本不存在或不可执行：$UNINSTALLER_SCRIPT"
    exit 1
fi

OUTPUT_DIR="$UNINSTALLER_BUILD_DIR" APP_NAME="$UNINSTALLER_APP_NAME" COMMAND_NAME="$UNINSTALLER_COMMAND_NAME" "$UNINSTALLER_SCRIPT"

UNINSTALLER_ARTIFACT_PATH=""
if [ -d "${UNINSTALLER_BUILD_DIR}/${UNINSTALLER_APP_NAME}" ]; then
    UNINSTALLER_ARTIFACT_PATH="${UNINSTALLER_BUILD_DIR}/${UNINSTALLER_APP_NAME}"
elif [ -f "${UNINSTALLER_BUILD_DIR}/${UNINSTALLER_COMMAND_NAME}" ]; then
    UNINSTALLER_ARTIFACT_PATH="${UNINSTALLER_BUILD_DIR}/${UNINSTALLER_COMMAND_NAME}"
fi

if [ -z "$UNINSTALLER_ARTIFACT_PATH" ]; then
    echo "❌ 错误：删除程序构建失败：$UNINSTALLER_BUILD_DIR"
    exit 1
fi

echo "🔨 正在生成 .pkg 安装包..."
pkgbuild \
    --root "$PAYLOAD_DIR" \
    --identifier "$IDENTIFIER" \
    --version "$VERSION" \
    --install-location "/" \
    --quiet \
    "$OUTPUT_PKG_PATH"

if [ ! -f "$OUTPUT_PKG_PATH" ]; then
    echo "❌ 错误：安装包生成失败：$OUTPUT_PKG_PATH"
    exit 1
fi

PKG_SIZE="$(du -h "$OUTPUT_PKG_PATH" | cut -f1)"
echo "📦 安装包大小: $PKG_SIZE"

if [ -n "$SIGN_IDENTITY" ]; then
    echo "🔏 使用 Developer ID Installer 证书签名安装包..."
    productsign --sign "$SIGN_IDENTITY" "$OUTPUT_PKG_PATH" "${RELEASE_DIR}/${SIGNED_PKG_NAME}"
    mv "${RELEASE_DIR}/${SIGNED_PKG_NAME}" "$OUTPUT_PKG_PATH"
else
    echo "ℹ️ 未设置 SIGN_IDENTITY，跳过安装包签名。"
fi

if [ -n "$SIGN_IDENTITY" ] && [ -n "$NOTARY_PROFILE" ]; then
    echo "📝 提交 Apple Notary 公证并等待结果..."
    xcrun notarytool submit "$OUTPUT_PKG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait

    echo "📌 正在 stapler 附票据..."
    xcrun stapler staple "$OUTPUT_PKG_PATH"
    xcrun stapler validate "$OUTPUT_PKG_PATH"
else
    echo "ℹ️ 未同时提供 SIGN_IDENTITY 和 NOTARY_PROFILE，跳过公证与 stapler。"
fi

echo "📦 整理分发目录..."
cp "$SOURCE_FILE" "${RELEASE_DIR}/SubFix.lua"
if [ -d "$UNINSTALLER_ARTIFACT_PATH" ]; then
    cp -R "$UNINSTALLER_ARTIFACT_PATH" "${RELEASE_DIR}/"
else
    cp "$UNINSTALLER_ARTIFACT_PATH" "${RELEASE_DIR}/"
fi

echo "🗜️ 生成分发 ZIP..."
ditto -c -k --sequesterRsrc --keepParent "$RELEASE_DIR" "$OUTPUT_ZIP_PATH"

echo "🧹 清理临时构建目录..."
rm -rf "$BUILD_ROOT"

echo ""
echo "✅ 安装包已生成：$OUTPUT_PKG_PATH"
echo "✅ 分发目录已生成：$RELEASE_DIR"
echo "✅ 分发 ZIP 已生成：$OUTPUT_ZIP_PATH"
echo ""
echo "💡 分发目录包含："
echo "   - 安装程序：${OUTPUT_PKG_NAME}"
echo "   - 源代码：SubFix.lua"
echo "   - 删除程序：$(basename "$UNINSTALLER_ARTIFACT_PATH")"
