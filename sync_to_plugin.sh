#!/bin/bash
# HooperAI 自动同步脚本 - 将当前 SubFix.lua 同步到 DaVinci Resolve 插件目录
# 使用方法: ./sync_to_plugin.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOLVE_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
HELPER_DIR="$RESOLVE_DIR/.subfix_support"
SOURCE_LUA="$SCRIPT_DIR/SubFix.lua"
SOURCE_GENERATOR_LUA="$SCRIPT_DIR/SubFix_GenerateSelectionSubtitles.lua"
SOURCE_ASR_HELPER="$SCRIPT_DIR/subfix_asr_transcribe.py"
SOURCE_ASR_SETUP="$SCRIPT_DIR/setup_asr_env.sh"

echo "🔄 正在同步到 DaVinci Resolve 插件目录..."
echo "📁 源文件: $SOURCE_LUA"
echo "📁 目标: $RESOLVE_DIR/SubFix.lua"

for required in "$SOURCE_LUA" "$SOURCE_GENERATOR_LUA" "$SOURCE_ASR_HELPER" "$SOURCE_ASR_SETUP"; do
  if [[ ! -f "$required" ]]; then
    echo "❌ 未找到源文件: $required"
    exit 1
  fi
done

# 创建目标目录（如果不存在）
mkdir -p "$RESOLVE_DIR" "$HELPER_DIR" 2>/dev/null

# 尝试直接复制
if cp "$SOURCE_LUA" "$RESOLVE_DIR/SubFix.lua" \
  && cp "$SOURCE_GENERATOR_LUA" "$RESOLVE_DIR/SubFix_GenerateSelectionSubtitles.lua" \
  && cp "$SOURCE_ASR_HELPER" "$HELPER_DIR/subfix_asr_transcribe.py" \
  && cp "$SOURCE_ASR_SETUP" "$HELPER_DIR/setup_asr_env.sh" 2>/dev/null; then
  chmod +x "$HELPER_DIR/setup_asr_env.sh" "$HELPER_DIR/subfix_asr_transcribe.py" 2>/dev/null || true
  rm -f "$RESOLVE_DIR/subfix_asr_transcribe.py" "$RESOLVE_DIR/setup_asr_env.sh" 2>/dev/null || true
  rm -rf "$RESOLVE_DIR/SubFix" "$RESOLVE_DIR/__pycache__" 2>/dev/null || true
  echo "✅ SubFix.lua 已同步 (直接复制)"
  echo "✅ SubFix_GenerateSelectionSubtitles.lua 已同步"
  echo "✅ ASR helper/setup 已同步"
  echo ""
  echo "🎉 同步完成。若 Resolve 已打开，可重载脚本或重启 Resolve 使改动生效。"
  exit 0
fi

# 如果直接复制失败，尝试用 rsync
if command -v rsync &> /dev/null; then
  if rsync -av "$SOURCE_LUA" "$RESOLVE_DIR/SubFix.lua" \
    && rsync -av "$SOURCE_GENERATOR_LUA" "$RESOLVE_DIR/SubFix_GenerateSelectionSubtitles.lua" \
    && rsync -av "$SOURCE_ASR_HELPER" "$HELPER_DIR/subfix_asr_transcribe.py" \
    && rsync -av "$SOURCE_ASR_SETUP" "$HELPER_DIR/setup_asr_env.sh" 2>/dev/null; then
    chmod +x "$HELPER_DIR/setup_asr_env.sh" "$HELPER_DIR/subfix_asr_transcribe.py" 2>/dev/null || true
    rm -f "$RESOLVE_DIR/subfix_asr_transcribe.py" "$RESOLVE_DIR/setup_asr_env.sh" 2>/dev/null || true
    rm -rf "$RESOLVE_DIR/SubFix" "$RESOLVE_DIR/__pycache__" 2>/dev/null || true
    echo "✅ SubFix.lua 已同步 (rsync)"
    echo "✅ SubFix_GenerateSelectionSubtitles.lua 已同步"
    echo "✅ ASR helper/setup 已同步"
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
echo "$HELPER_DIR"
echo ""
echo "或者在终端运行："
echo "cp \"$SOURCE_LUA\" \"$RESOLVE_DIR/SubFix.lua\""
echo "cp \"$SOURCE_GENERATOR_LUA\" \"$RESOLVE_DIR/SubFix_GenerateSelectionSubtitles.lua\""
echo "mkdir -p \"$HELPER_DIR\""
echo "cp \"$SOURCE_ASR_HELPER\" \"$HELPER_DIR/subfix_asr_transcribe.py\""
echo "cp \"$SOURCE_ASR_SETUP\" \"$HELPER_DIR/setup_asr_env.sh\""
echo "rm -f \"$RESOLVE_DIR/subfix_asr_transcribe.py\" \"$RESOLVE_DIR/setup_asr_env.sh\""
echo "rm -rf \"$RESOLVE_DIR/SubFix\" \"$RESOLVE_DIR/__pycache__\""
exit 1
