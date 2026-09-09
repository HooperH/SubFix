#!/bin/bash
set -euo pipefail

finish() {
  if [[ -t 0 ]]; then read -r -p "按回车关闭窗口…" answer || true; fi
}
trap 'echo "卸载未完成。请检查上方错误与目录权限后重试。" >&2; finish' ERR

case "${1:-}" in
  ""|--dry-run|--needs-admin) ;;
  *) echo "用法：卸载_SubFix.command [--dry-run|--needs-admin]" >&2; exit 2 ;;
esac
[[ "${HOME:-}" == /* && "$HOME" != / ]] || { echo "无法确定用户目录。" >&2; exit 1; }
USER_UTILITY="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SYSTEM_UTILITY="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
user_targets=()
system_targets=()
# Fixed SubFix-owned entries only; other Resolve scripts and user backups remain.
for name in SubFix .subfix_support SubFix.lua SubFix_GenerateSelectionSubtitles.lua; do
  if [[ -e "$USER_UTILITY/$name" || -L "$USER_UTILITY/$name" ]]; then
    user_targets+=("$USER_UTILITY/$name")
  fi
  if [[ -e "$SYSTEM_UTILITY/$name" || -L "$SYSTEM_UTILITY/$name" ]]; then
    system_targets+=("$SYSTEM_UTILITY/$name")
  fi
done
# Qwen dependencies now live outside Resolve's recursively scanned script tree.
for name in envs/qwen-local .subfix-qwen-local-ready.json; do
  path="$HOME/Library/Application Support/SubFix/$name"
  if [[ -e "$path" || -L "$path" ]]; then user_targets+=("$path"); fi
done
# Read-only probe for the app wrapper, before any prompt or mutation.
if [[ "${1:-}" == --needs-admin ]]; then
  if (( ${#system_targets[@]} )); then echo yes; else echo no; fi
  exit 0
fi
if (( ${#user_targets[@]} + ${#system_targets[@]} == 0 )); then
  echo "没有发现已安装的 SubFix。"
  finish
  exit 0
fi

echo "将删除以下 SubFix 插件文件及其内置运行环境："
if (( ${#user_targets[@]} )); then printf '%s\n' "${user_targets[@]}"; fi
if (( ${#system_targets[@]} )); then printf '%s\n' "${system_targets[@]}"; fi
echo "保留达芬奇项目，以及用户数据目录中的字幕备份和识别模型。"
if [[ "${1:-}" == --dry-run ]]; then exit 0; fi

echo "请先关闭 SubFix 窗口。输入 UNINSTALL 确认卸载，其他输入均取消："
read -r confirmation || confirmation=""
if [[ "$confirmation" != UNINSTALL ]]; then
  echo "已取消，未删除文件。"
  exit 0
fi
# Request administrator access only when a system-wide installation exists.
# Authenticate before removing anything so cancellation leaves both installs intact.
if (( ${#system_targets[@]} )); then
  echo "检测到系统目录安装，卸载这部分需要管理员密码。"
  sudo -v
fi
if (( ${#user_targets[@]} )); then /bin/rm -rf -- "${user_targets[@]}"; fi
if (( ${#system_targets[@]} )); then sudo /bin/rm -rf -- "${system_targets[@]}"; fi
echo "SubFix 卸载完成，请重新打开达芬奇以刷新脚本菜单。"
finish
