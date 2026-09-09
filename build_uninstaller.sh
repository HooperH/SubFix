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
TARGET_DIR="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SUBFIX_MENU_DIR="${TARGET_DIR}/SubFix"
TARGET_PATH="${SUBFIX_MENU_DIR}/SubFix.lua"
LEGACY_MAIN_PATH="${TARGET_DIR}/SubFix.lua"
GENERATOR_PATH="${SUBFIX_MENU_DIR}/生成选区字幕.lua"
LEGACY_SUBFIX_GENERATOR_PATH="${SUBFIX_MENU_DIR}/SubFix_GenerateSelectionSubtitles.lua"
LEGACY_GENERATOR_PATH="${TARGET_DIR}/SubFix_GenerateSelectionSubtitles.lua"
SUPPORT_DIR="${TARGET_DIR}/.subfix_support"
GENERATE_CORE_PATH="${SUPPORT_DIR}/subfix_generate_selection_core.lua"
ASR_HELPER_PATH="${SUPPORT_DIR}/subfix_asr_transcribe.py"
ASR_SETUP_PATH="${SUPPORT_DIR}/setup_asr_env.sh"

echo "🗑️ 开始构建删除程序..."

mkdir -p "$OUTPUT_DIR"
rm -rf "$APP_PATH"
rm -f "$COMMAND_PATH"

build_app() {
    if ! command -v osacompile >/dev/null 2>&1; then
        return 1
    fi

    cat > "$TMP_SCRIPT" <<'EOF'
set subfixMenuDir to "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix"
set targetPath to subfixMenuDir & "/SubFix.lua"
set legacyMainPath to "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix.lua"
set generatorPath to subfixMenuDir & "/生成选区字幕.lua"
set legacySubfixGeneratorPath to subfixMenuDir & "/SubFix_GenerateSelectionSubtitles.lua"
set legacyGeneratorPath to "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix_GenerateSelectionSubtitles.lua"
set supportDir to "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.subfix_support"
set generateCorePath to supportDir & "/subfix_generate_selection_core.lua"
set asrHelperPath to supportDir & "/subfix_asr_transcribe.py"
set asrSetupPath to supportDir & "/setup_asr_env.sh"
set userHome to POSIX path of (path to home folder)
set userUtility to userHome & "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
set userSupport to userUtility & "/.subfix_support"
set commandText to "rm -f " & quoted form of targetPath & " " & quoted form of legacyMainPath & " " & quoted form of generatorPath & " " & quoted form of legacySubfixGeneratorPath & " " & quoted form of legacyGeneratorPath & " " & quoted form of generateCorePath & " " & quoted form of asrHelperPath & " " & quoted form of asrSetupPath & "; rmdir " & quoted form of subfixMenuDir & " 2>/dev/null || true; rm -rf " & quoted form of supportDir & " " & quoted form of (userUtility & "/SubFix") & " " & quoted form of userSupport
do shell script commandText with administrator privileges
EOF

    osacompile -l AppleScript -o "$APP_PATH" "$TMP_SCRIPT" >/dev/null 2>&1
}

build_command() {
    cat > "$COMMAND_PATH" <<EOF
#!/bin/bash
set -euo pipefail

TARGET_PATH="$TARGET_PATH"
LEGACY_MAIN_PATH="$LEGACY_MAIN_PATH"
SUBFIX_MENU_DIR="$SUBFIX_MENU_DIR"
GENERATOR_PATH="$GENERATOR_PATH"
LEGACY_SUBFIX_GENERATOR_PATH="$LEGACY_SUBFIX_GENERATOR_PATH"
LEGACY_GENERATOR_PATH="$LEGACY_GENERATOR_PATH"
SUPPORT_DIR="$SUPPORT_DIR"
GENERATE_CORE_PATH="$GENERATE_CORE_PATH"
ASR_HELPER_PATH="$ASR_HELPER_PATH"
ASR_SETUP_PATH="$ASR_SETUP_PATH"
USER_UTILITY="\$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
USER_SUPPORT="\$USER_UTILITY/.subfix_support"

echo "即将删除："
echo "\$TARGET_PATH"
echo "\$LEGACY_MAIN_PATH"
echo "\$GENERATOR_PATH"
echo "\$LEGACY_SUBFIX_GENERATOR_PATH"
echo "\$LEGACY_GENERATOR_PATH"
echo "\$GENERATE_CORE_PATH"
echo "\$ASR_HELPER_PATH"
echo "\$ASR_SETUP_PATH"
echo "\$SUPPORT_DIR"
echo "\$USER_UTILITY/SubFix"
echo "\$USER_SUPPORT"
if sudo rm -f "\$TARGET_PATH" "\$LEGACY_MAIN_PATH" "\$GENERATOR_PATH" "\$LEGACY_SUBFIX_GENERATOR_PATH" "\$LEGACY_GENERATOR_PATH" "\$GENERATE_CORE_PATH" "\$ASR_HELPER_PATH" "\$ASR_SETUP_PATH" && { sudo rmdir "\$SUBFIX_MENU_DIR" 2>/dev/null || true; } && sudo rm -rf "\$SUPPORT_DIR" "\$USER_UTILITY/SubFix" "\$USER_SUPPORT"; then
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
