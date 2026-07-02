#!/bin/bash
set -euo pipefail

TARGET_PATH="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/SubFix.lua"

echo "即将删除：$TARGET_PATH"
if sudo rm -f "$TARGET_PATH"; then
    echo ""
    echo "✅ SubFix 卸载成功，请重启 DaVinci Resolve。"
else
    echo ""
    echo "❌ 卸载失败，请检查密码或手动删除文件。"
    exit 1
fi

echo ""
read -r -p "按回车键退出..." _
