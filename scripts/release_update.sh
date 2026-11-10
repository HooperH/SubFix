#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"
NOTES_FILE="${2:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "用法: $0 3.1.0 更新说明.md"; exit 2; }
[[ -n "$NOTES_FILE" && -f "$NOTES_FILE" ]] || { echo "请提供更新说明文件"; exit 2; }
if [[ "$VERSION" == "3.4.0" ]]; then
  echo "拒绝发布增量包：3.4.0 必须使用完整 PKG 覆盖升级，旧更新器无法安装新增模型中心文件。" >&2
  exit 2
fi
cd "$ROOT"
[[ -z "$(git status --porcelain)" ]] || { echo "拒绝发布：工作树不干净"; exit 1; }
git grep -q "SUBFIX_VERSION = \"$VERSION\"" -- SubFix.lua || { echo "拒绝发布：SubFix.lua 版本不匹配"; exit 1; }
command -v gh >/dev/null || { echo "未找到 gh，请先安装并执行 gh auth login"; exit 1; }
gh auth status >/dev/null
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m pytest -q

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FFMPEG_STAGE_DIR="${SUBFIX_FFMPEG_STAGE_DIR:-$WORK/ffmpeg}"
if [[ -z "${SUBFIX_FFMPEG_STAGE_DIR:-}" ]]; then
  "$ROOT/scripts/build_bundled_ffmpeg.sh" "$FFMPEG_STAGE_DIR"
fi
for required in ffmpeg FFmpeg-LGPL-2.1.txt ffmpeg-9.0.tar.xz; do
  [[ -f "$FFMPEG_STAGE_DIR/$required" ]] || { echo "FFmpeg 发布产物不完整：$FFMPEG_STAGE_DIR/$required" >&2; exit 1; }
done
[[ -x "$FFMPEG_STAGE_DIR/ffmpeg" ]] || { echo "FFmpeg 发布产物不可执行：$FFMPEG_STAGE_DIR/ffmpeg" >&2; exit 1; }
cp "$FFMPEG_STAGE_DIR/ffmpeg" "$WORK/subfix-ffmpeg"
cp "$FFMPEG_STAGE_DIR/FFmpeg-LGPL-2.1.txt" "$WORK/FFmpeg-LGPL-2.1.txt"
PAYLOAD="$WORK/payload"
mkdir -p "$PAYLOAD/SubFix" "$PAYLOAD/.subfix_support"
FILES=(
  "SubFix/SubFix.lua:SubFix.lua"
  "SubFix/生成选区字幕.lua:生成选区字幕.lua"
  ".subfix_support/subfix_generate_selection_core.lua:.subfix_support/subfix_generate_selection_core.lua"
  ".subfix_support/subfix_model_manager.py:.subfix_support/subfix_model_manager.py"
  ".subfix_support/subfix_model_center.lua:.subfix_support/subfix_model_center.lua"
  ".subfix_support/subfix_process_group.py:.subfix_support/subfix_process_group.py"
  ".subfix_support/subfix_update.py:.subfix_support/subfix_update.py"
  ".subfix_support/subfix_asr_transcribe.py:subfix_asr_transcribe.py"
  ".subfix_support/subfix_generate_v4.py:subfix_generate_v4.py"
  ".subfix_support/subfix_generate_v5.py:subfix_generate_v5.py"
  ".subfix_support/subfix_generate_textnorm.py:subfix_generate_textnorm.py"
  ".subfix_support/setup_asr_env.sh:setup_asr_env.sh"
  ".subfix_support/segmentation_profile.json:.subfix_support/segmentation_profile.json"
  ".subfix_support/segmentation_profile_v3.json:.subfix_support/segmentation_profile_v3.json"
  ".subfix_support/segmentation_profile_v4.json:.subfix_support/segmentation_profile_v4.json"
  ".subfix_support/bin/ffmpeg:$WORK/subfix-ffmpeg"
  ".subfix_support/licenses/FFmpeg-LGPL-2.1.txt:$WORK/FFmpeg-LGPL-2.1.txt"
  ".subfix_support/subfix_qwen_local_manager.py:.subfix_support/subfix_qwen_local_manager.py"
)
for pair in "${FILES[@]}"; do
  target="${pair%%:*}"; source="${pair#*:}"
  mkdir -p "$PAYLOAD/$(dirname "$target")"
  cp "$source" "$PAYLOAD/$target"
done
if [[ -f "$ROOT/.subfix_support/model-downloads.json" ]]; then
  cp "$ROOT/.subfix_support/model-downloads.json" "$PAYLOAD/.subfix_support/model-downloads.json"
fi
python3 "$ROOT/scripts/check_release_privacy.py" "$PAYLOAD"
# Default asset names must remain readable by every shipped updater, starting at v3.1.0.
python3 "$ROOT/scripts/build_update_archives.py" "$PAYLOAD" "$VERSION" "$WORK"
ASSET_PREFIX="SubFix-update-v${VERSION}"
gh release create "v$VERSION" \
  "$WORK/$ASSET_PREFIX.zip" "$WORK/$ASSET_PREFIX.sha256" \
  "$WORK/$ASSET_PREFIX-full.zip" "$WORK/$ASSET_PREFIX-full.sha256" \
  "$FFMPEG_STAGE_DIR/ffmpeg-9.0.tar.xz" --verify-tag --target "$(git rev-parse HEAD)" --title "SubFix v$VERSION" --notes-file "$NOTES_FILE"
