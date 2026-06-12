#!/bin/bash
# HooperAI 自动同步脚本 - 将当前 SubFix.lua 同步到 DaVinci Resolve 插件目录
# 使用方法: ./sync_to_plugin.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOLVE_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SOURCE_LUA="$SCRIPT_DIR/SubFix.lua"

echo "🔄 正在同步到 DaVinci Resolve 插件目录..."
echo "📁 源文件: $SOURCE_LUA"
echo "📁 目标: $RESOLVE_DIR/SubFix.lua"

if [[ ! -f "$SOURCE_LUA" ]]; then
  echo "❌ 未找到源文件: $SOURCE_LUA"
  exit 1
fi

# 创建目标目录（如果不存在）
mkdir -p "$RESOLVE_DIR" 2>/dev/null

# 尝试直接复制
if cp "$SOURCE_LUA" "$RESOLVE_DIR/SubFix.lua" 2>/dev/null; then
  echo "✅ SubFix.lua 已同步 (直接复制)"
  echo ""
  echo "🎉 同步完成。若 Resolve 已打开，可重载脚本或重启 Resolve 使改动生效。"
  exit 0
fi

# 如果直接复制失败，尝试用 rsync
if command -v rsync &> /dev/null; then
  if rsync -av "$SOURCE_LUA" "$RESOLVE_DIR/SubFix.lua" 2>/dev/null; then
    echo "✅ SubFix.lua 已同步 (rsync)"
    echo ""
    echo "🎉 同步完成。若 Resolve 已打开，可重载脚本或重启 Resolve 使改动生效。"
    exit 0
  fi
fi

# 如果都失败，提供手动复制说明
echo ""
echo "⚠️ 自动复制失败，请手动复制文件："
echo ""
echo "源文件路径："
echo "$SOURCE_LUA"
echo ""
echo "目标目录："
echo "$RESOLVE_DIR"
echo ""
echo "或者在终端运行："
echo "cp \"$SOURCE_LUA\" \"$RESOLVE_DIR/SubFix.lua\""
