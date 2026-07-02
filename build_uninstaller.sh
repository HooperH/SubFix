#!/bin/bash

# =============================================================================
# SubFix - 删除程序构建脚本
# 功能：优先生成 macOS .app；若当前环境不支持，则回退为 .command
# =============================================================================

set -euo pipefail

OUTPUT_DIR="${OUTPUT_DIR:-$(pwd)}"
APP_NAME="${APP_NAME:-卸载_SubFix.app}"
COMMAND_NAME="${COMMAND_NAME:-卸载_SubFix.command}"
APP_PATH="${OUTPUT_DIR}/${APP_NAME}"
COMMAND_PATH="${OUTPUT_DIR}/${COMMAND_NAME}"
TMP_SCRIPT="${TMPDIR:-/tmp}/SubFix_uninstall.applescript"
TARGET_PATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix.lua"

echo "🗑️ 开始构建删除程序..."

mkdir -p "$OUTPUT_DIR"
rm -rf "$APP_PATH"
rm -f "$COMMAND_PATH"

build_app() {
    if ! command -v osacompile >/dev/null 2>&1; then
        return 1
    fi

    cat > "$TMP_SCRIPT" <<'EOF'
set targetPath to "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix.lua"
set commandText to "rm -f " & quoted form of targetPath
do shell script commandText with administrator privileges
EOF

    osacompile -l AppleScript -o "$APP_PATH" "$TMP_SCRIPT" >/dev/null 2>&1
}

build_command() {
    cat > "$COMMAND_PATH" <<EOF
#!/bin/bash
set -euo pipefail

TARGET_PATH="$TARGET_PATH"

echo "即将删除：\$TARGET_PATH"
if sudo rm -f "\$TARGET_PATH"; then
    echo ""
    echo "✅ SubFix 卸载成功，请重启 DaVinci Resolve。"
else
    echo ""
    echo "❌ 卸载失败，请检查密码或手动删除文件。"
    exit 1
fi

echo ""
read -r -p "按回车键退出..." _
EOF

    chmod +x "$COMMAND_PATH"
}

if build_app; then
    echo "✅ 删除程序已生成：$APP_PATH"
else
    echo "ℹ️ 当前环境无法稳定生成 .app，回退为 .command 删除程序。"
    build_command
    echo "✅ 删除程序已生成：$COMMAND_PATH"
fi

echo ""
echo "💡 使用说明："
if [ -d "$APP_PATH" ]; then
    echo "   直接双击【${APP_NAME}】即可运行。"
else
    echo "   直接双击【${COMMAND_NAME}】即可运行。"
    echo "   终端会要求输入管理员密码以删除插件文件。"
fi
