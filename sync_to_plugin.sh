#!/bin/bash
# HooperAI 自动同步脚本 - 将当前 SubFix.lua 同步到 DaVinci Resolve 插件目录
# 使用方法: ./sync_to_plugin.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOLVE_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
HELPER_DIR="$RESOLVE_DIR/.subfix_support"
SUBFIX_MENU_DIR="$RESOLVE_DIR/SubFix"
SOURCE_LUA="$SCRIPT_DIR/SubFix.lua"
SOURCE_GENERATOR_LUA="$SCRIPT_DIR/生成选区字幕.lua"
SOURCE_GENERATE_CORE="$SCRIPT_DIR/.subfix_support/subfix_generate_selection_core.lua"
SOURCE_ASR_HELPER="$SCRIPT_DIR/subfix_asr_transcribe.py"
SOURCE_GENERATE_V4="$SCRIPT_DIR/subfix_generate_v4.py"
SOURCE_GENERATE_V5="$SCRIPT_DIR/subfix_generate_v5.py"
SOURCE_GENERATE_TEXTNORM="$SCRIPT_DIR/subfix_generate_textnorm.py"
SOURCE_ASR_SETUP="$SCRIPT_DIR/setup_asr_env.sh"
SOURCE_SEGMENTATION_PROFILE="$SCRIPT_DIR/.subfix_support/segmentation_profile.json"
SOURCE_SEGMENTATION_PROFILE_V3="$SCRIPT_DIR/.subfix_support/segmentation_profile_v3.json"
SOURCE_SEGMENTATION_PROFILE_V4="$SCRIPT_DIR/.subfix_support/segmentation_profile_v4.json"
SOURCE_QWEN_CPP_DIR="$SCRIPT_DIR/.subfix_support/qwen3-asr.cpp"
SOURCE_QWEN_MODELS_DIR="$SCRIPT_DIR/.subfix_support/models"

link_qwen_support() {
  if [[ -x "$SOURCE_QWEN_CPP_DIR/build/qwen3-asr-cli" ]]; then
    rm -rf "$HELPER_DIR/qwen3-asr.cpp" 2>/dev/null || true
    ln -s "$SOURCE_QWEN_CPP_DIR" "$HELPER_DIR/qwen3-asr.cpp" 2>/dev/null || true
  fi
  if compgen -G "$SOURCE_QWEN_MODELS_DIR/qwen3-forced-aligner-0.6b-*.gguf" > /dev/null; then
    rm -rf "$HELPER_DIR/models" 2>/dev/null || true
    ln -s "$SOURCE_QWEN_MODELS_DIR" "$HELPER_DIR/models" 2>/dev/null || true
  fi
}

echo "🔄 正在同步到 DaVinci Resolve 插件目录..."
echo "📁 源文件: $SOURCE_LUA"
echo "📁 目标: $SUBFIX_MENU_DIR/SubFix.lua"

for required in "$SOURCE_LUA" "$SOURCE_GENERATOR_LUA" "$SOURCE_GENERATE_CORE" "$SOURCE_ASR_HELPER" "$SOURCE_GENERATE_V4" "$SOURCE_GENERATE_V5" "$SOURCE_GENERATE_TEXTNORM" "$SOURCE_ASR_SETUP" "$SOURCE_SEGMENTATION_PROFILE" "$SOURCE_SEGMENTATION_PROFILE_V3" "$SOURCE_SEGMENTATION_PROFILE_V4"; do
  if [[ ! -f "$required" ]]; then
    echo "❌ 未找到源文件: $required"
    exit 1
  fi
done

# 创建目标目录（如果不存在）
mkdir -p "$RESOLVE_DIR" "$HELPER_DIR" "$SUBFIX_MENU_DIR" 2>/dev/null

# 尝试直接复制
if cp "$SOURCE_LUA" "$SUBFIX_MENU_DIR/SubFix.lua" \
  && cp "$SOURCE_GENERATOR_LUA" "$SUBFIX_MENU_DIR/生成选区字幕.lua" \
  && cp "$SOURCE_GENERATE_CORE" "$HELPER_DIR/subfix_generate_selection_core.lua" \
  && cp "$SOURCE_ASR_HELPER" "$HELPER_DIR/subfix_asr_transcribe.py" \
  && cp "$SOURCE_GENERATE_V4" "$HELPER_DIR/subfix_generate_v4.py" \
  && cp "$SOURCE_GENERATE_V5" "$HELPER_DIR/subfix_generate_v5.py" \
  && cp "$SOURCE_GENERATE_TEXTNORM" "$HELPER_DIR/subfix_generate_textnorm.py" \
  && cp "$SOURCE_ASR_SETUP" "$HELPER_DIR/setup_asr_env.sh" \
  && cp "$SOURCE_SEGMENTATION_PROFILE" "$HELPER_DIR/segmentation_profile.json" \
  && cp "$SOURCE_SEGMENTATION_PROFILE_V3" "$HELPER_DIR/segmentation_profile_v3.json" \
  && cp "$SOURCE_SEGMENTATION_PROFILE_V4" "$HELPER_DIR/segmentation_profile_v4.json" 2>/dev/null; then
  chmod +x "$HELPER_DIR/setup_asr_env.sh" "$HELPER_DIR/subfix_asr_transcribe.py" 2>/dev/null || true
  link_qwen_support
  rm -f "$RESOLVE_DIR/SubFix.lua" "$RESOLVE_DIR/SubFix_GenerateSelectionSubtitles.lua" "$SUBFIX_MENU_DIR/SubFix_GenerateSelectionSubtitles.lua" "$RESOLVE_DIR/subfix_asr_transcribe.py" "$RESOLVE_DIR/setup_asr_env.sh" 2>/dev/null || true
  rm -rf "$RESOLVE_DIR/__pycache__" 2>/dev/null || true
  echo "✅ SubFix/SubFix.lua 已同步 (直接复制)"
  echo "✅ SubFix/生成选区字幕.lua 已同步"
  echo "✅ 生成字幕 core 已同步"
  echo "✅ ASR helper/setup 已同步"
  echo "✅ Qwen3 对齐引擎链接已同步"
  echo ""
  echo "🎉 同步完成。若 Resolve 已打开，可重载脚本或重启 Resolve 使改动生效。"
  exit 0
fi

# 如果直接复制失败，尝试用 rsync
if command -v rsync &> /dev/null; then
  if rsync -av "$SOURCE_LUA" "$SUBFIX_MENU_DIR/SubFix.lua" \
    && rsync -av "$SOURCE_GENERATOR_LUA" "$SUBFIX_MENU_DIR/生成选区字幕.lua" \
    && rsync -av "$SOURCE_GENERATE_CORE" "$HELPER_DIR/subfix_generate_selection_core.lua" \
    && rsync -av "$SOURCE_ASR_HELPER" "$HELPER_DIR/subfix_asr_transcribe.py" \
    && rsync -av "$SOURCE_GENERATE_V4" "$HELPER_DIR/subfix_generate_v4.py" \
    && rsync -av "$SOURCE_GENERATE_V5" "$HELPER_DIR/subfix_generate_v5.py" \
    && rsync -av "$SOURCE_GENERATE_TEXTNORM" "$HELPER_DIR/subfix_generate_textnorm.py" \
    && rsync -av "$SOURCE_ASR_SETUP" "$HELPER_DIR/setup_asr_env.sh" \
    && rsync -av "$SOURCE_SEGMENTATION_PROFILE" "$HELPER_DIR/segmentation_profile.json" \
    && rsync -av "$SOURCE_SEGMENTATION_PROFILE_V3" "$HELPER_DIR/segmentation_profile_v3.json" \
    && rsync -av "$SOURCE_SEGMENTATION_PROFILE_V4" "$HELPER_DIR/segmentation_profile_v4.json" 2>/dev/null; then
    chmod +x "$HELPER_DIR/setup_asr_env.sh" "$HELPER_DIR/subfix_asr_transcribe.py" 2>/dev/null || true
    link_qwen_support
    rm -f "$RESOLVE_DIR/SubFix.lua" "$RESOLVE_DIR/SubFix_GenerateSelectionSubtitles.lua" "$SUBFIX_MENU_DIR/SubFix_GenerateSelectionSubtitles.lua" "$RESOLVE_DIR/subfix_asr_transcribe.py" "$RESOLVE_DIR/setup_asr_env.sh" 2>/dev/null || true
    rm -rf "$RESOLVE_DIR/__pycache__" 2>/dev/null || true
    echo "✅ SubFix/SubFix.lua 已同步 (rsync)"
    echo "✅ SubFix/生成选区字幕.lua 已同步"
    echo "✅ 生成字幕 core 已同步"
    echo "✅ ASR helper/setup 已同步"
    echo "✅ Qwen3 对齐引擎链接已同步"
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
echo "mkdir -p \"$HELPER_DIR\" \"$SUBFIX_MENU_DIR\""
echo "cp \"$SOURCE_LUA\" \"$SUBFIX_MENU_DIR/SubFix.lua\""
echo "cp \"$SOURCE_GENERATOR_LUA\" \"$SUBFIX_MENU_DIR/生成选区字幕.lua\""
echo "cp \"$SOURCE_GENERATE_CORE\" \"$HELPER_DIR/subfix_generate_selection_core.lua\""
echo "cp \"$SOURCE_ASR_HELPER\" \"$HELPER_DIR/subfix_asr_transcribe.py\""
echo "cp \"$SOURCE_GENERATE_V4\" \"$HELPER_DIR/subfix_generate_v4.py\""
echo "cp \"$SOURCE_GENERATE_V5\" \"$HELPER_DIR/subfix_generate_v5.py\""
echo "cp \"$SOURCE_GENERATE_TEXTNORM\" \"$HELPER_DIR/subfix_generate_textnorm.py\""
echo "cp \"$SOURCE_ASR_SETUP\" \"$HELPER_DIR/setup_asr_env.sh\""
echo "cp \"$SOURCE_SEGMENTATION_PROFILE\" \"$HELPER_DIR/segmentation_profile.json\""
echo "cp \"$SOURCE_SEGMENTATION_PROFILE_V3\" \"$HELPER_DIR/segmentation_profile_v3.json\""
echo "cp \"$SOURCE_SEGMENTATION_PROFILE_V4\" \"$HELPER_DIR/segmentation_profile_v4.json\""
echo "ln -sfn \"$SOURCE_QWEN_CPP_DIR\" \"$HELPER_DIR/qwen3-asr.cpp\""
echo "ln -sfn \"$SOURCE_QWEN_MODELS_DIR\" \"$HELPER_DIR/models\""
echo "rm -f \"$RESOLVE_DIR/SubFix.lua\" \"$RESOLVE_DIR/SubFix_GenerateSelectionSubtitles.lua\" \"$SUBFIX_MENU_DIR/SubFix_GenerateSelectionSubtitles.lua\" \"$RESOLVE_DIR/subfix_asr_transcribe.py\" \"$RESOLVE_DIR/setup_asr_env.sh\""
echo "rm -rf \"$RESOLVE_DIR/__pycache__\""
exit 1
