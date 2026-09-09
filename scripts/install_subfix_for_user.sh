#!/bin/bash
# Deploy the complete SubFix bundle to Resolve's per-user script directory.
set -euo pipefail

PATH=/usr/bin:/bin:/usr/sbin:/sbin
SYSTEM_UTILITY="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SYSTEM_SUPPORT="$SYSTEM_UTILITY/.subfix_support"
RUNTIME_PYTHON="$SYSTEM_SUPPORT/runtime/python/bin/python3"
BUNDLED_FFMPEG="$SYSTEM_SUPPORT/bin/ffmpeg"

cleanup_system_subfix_menu() {
  local legacy_path
  # The package payload is staged system-wide so postinstall can run as root,
  # but Resolve scans both system and user script roots. Keep only the user copy.
  if [[ -L "$SYSTEM_UTILITY/SubFix" ]]; then
    rm -f -- "$SYSTEM_UTILITY/SubFix"
  else
    for legacy_path in \
      "$SYSTEM_UTILITY/SubFix/SubFix.lua" \
      "$SYSTEM_UTILITY/SubFix/生成选区字幕.lua" \
      "$SYSTEM_UTILITY/SubFix/SubFix_GenerateSelectionSubtitles.lua"; do
      [[ -f "$legacy_path" || -L "$legacy_path" ]] && rm -f -- "$legacy_path"
    done
    rmdir "$SYSTEM_UTILITY/SubFix" 2>/dev/null || true
  fi
  for legacy_path in \
    "$SYSTEM_UTILITY/SubFix.lua" \
    "$SYSTEM_UTILITY/SubFix_GenerateSelectionSubtitles.lua"; do
    [[ -f "$legacy_path" || -L "$legacy_path" ]] && rm -f -- "$legacy_path"
  done
  if [[ -L "$SYSTEM_SUPPORT" ]]; then
    rm -f -- "$SYSTEM_SUPPORT"
  elif [[ -d "$SYSTEM_SUPPORT" ]]; then
    rm -rf -- "$SYSTEM_SUPPORT"
  fi
  return 0
}

if [[ ! -x "$RUNTIME_PYTHON" || ! -x "$BUNDLED_FFMPEG" || ! -d "$SYSTEM_UTILITY/SubFix" ]]; then
  echo "SubFix 安装不完整：未找到内置运行时、ffmpeg 或主脚本目录。" >&2
  exit 1
fi

CONSOLE_USER="$(stat -f '%Su' /dev/console)"
case "$CONSOLE_USER" in
  ""|root|loginwindow)
    echo "未检测到图形界面登录用户；已保留系统级 SubFix 安装。"
    exit 0
    ;;
esac

USER_HOME="$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory | awk '{print $2}')"
if [[ -z "$USER_HOME" || ! -d "$USER_HOME" ]]; then
  echo "无法确定当前登录用户 $CONSOLE_USER 的主目录。" >&2
  exit 1
fi

USER_UTILITY="$USER_HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
USER_SUPPORT="$USER_UTILITY/.subfix_support"
mkdir -p "$USER_UTILITY"

for target in "$USER_UTILITY/SubFix" "$USER_SUPPORT" "$USER_SUPPORT/runtime" "$USER_SUPPORT/bin" "$USER_SUPPORT/models"; do
  if [[ -L "$target" ]]; then
    echo "拒绝跟随旧版 SubFix 符号链接：$target" >&2
    exit 1
  fi
done

# ditto merges the new package files without deleting private Qwen models,
# environments, credentials, or preferences left by an earlier installation.
ditto "$SYSTEM_UTILITY/SubFix" "$USER_UTILITY/SubFix"
ditto "$SYSTEM_SUPPORT" "$USER_SUPPORT"

chown -R "$CONSOLE_USER":staff "$USER_UTILITY/SubFix" "$USER_SUPPORT/runtime" "$USER_SUPPORT/bin"
[[ -d "$USER_SUPPORT/licenses" ]] && chown -R "$CONSOLE_USER":staff "$USER_SUPPORT/licenses"
chown "$CONSOLE_USER":staff "$USER_SUPPORT"
for support_file in \
  subfix_generate_selection_core.lua \
  subfix_update.py \
  subfix_qwen_local_manager.py \
  subfix_process_group.py \
  subfix_asr_transcribe.py \
  subfix_generate_v4.py \
  subfix_generate_v5.py \
  subfix_generate_textnorm.py \
  setup_asr_env.sh \
  segmentation_profile.json \
  segmentation_profile_v3.json \
  segmentation_profile_v4.json \
  models/qwen3-forced-aligner-0.6b-f16.gguf; do
  [[ -e "$USER_SUPPORT/$support_file" ]] && chown "$CONSOLE_USER":staff "$USER_SUPPORT/$support_file"
done

cleanup_system_subfix_menu

echo "已部署完整 SubFix 运行时到 ${USER_UTILITY}（用户：${CONSOLE_USER}）。"
