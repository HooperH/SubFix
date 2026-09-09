#!/bin/bash
set -euo pipefail

SOURCE_BUILD="${1:?usage: stage_qwen3_cpp_runtime.sh SOURCE_BUILD DEST_BIN}"
DEST_BIN="${2:?usage: stage_qwen3_cpp_runtime.sh SOURCE_BUILD DEST_BIN}"
SOURCE_CLI="$SOURCE_BUILD/qwen3-asr-cli"
[[ -f "$SOURCE_CLI" ]] || { echo "missing qwen3-asr-cli: $SOURCE_CLI" >&2; exit 1; }

DEST_PARENT="$(dirname "$DEST_BIN")"
DEST_NAME="$(basename "$DEST_BIN")"
mkdir -p "$DEST_PARENT"
STAGING="$(mktemp -d "${DEST_PARENT}/.${DEST_NAME}.staging.XXXXXX")"
cleanup() {
  if [[ -n "${STAGING:-}" && -d "$STAGING" ]]; then
    rm -rf "$STAGING"
  fi
  return 0
}
trap cleanup EXIT

cp "$SOURCE_CLI" "$STAGING/qwen3-asr-cli"
shopt -s nullglob
DYLIBS=("$SOURCE_BUILD"/*.dylib)
(( ${#DYLIBS[@]} > 0 )) || { echo "qwen3-asr.cpp build contains no dylibs" >&2; exit 1; }
cp "${DYLIBS[@]}" "$STAGING/"
chmod 755 "$STAGING/qwen3-asr-cli"

list_rpaths() {
  otool -l "$1" | awk '
    $1 == "cmd" && $2 == "LC_RPATH" { need_path = 1; next }
    need_path && $1 == "path" {
      line = $0
      sub(/^[[:space:]]*path[[:space:]]+/, "", line)
      sub(/[[:space:]]+\(offset[[:space:]][0-9]+\)$/, "", line)
      print line
      need_path = 0
    }
  '
}

for binary in "$STAGING/qwen3-asr-cli" "$STAGING"/*.dylib; do
  [[ -L "$binary" ]] && continue
  while IFS= read -r rpath; do
    case "$rpath" in
      /*) install_name_tool -delete_rpath "$rpath" "$binary" ;;
    esac
  done < <(list_rpaths "$binary")
done

if ! list_rpaths "$STAGING/qwen3-asr-cli" | grep -Fxq '@executable_path'; then
  install_name_tool -add_rpath '@executable_path' "$STAGING/qwen3-asr-cli"
fi

for binary in "$STAGING/qwen3-asr-cli" "$STAGING"/*.dylib; do
  file "$binary" | grep -q arm64 || { echo "non-arm64 binary: $binary" >&2; exit 1; }
  otool -L "$binary" | tail -n +2 | grep -q '/Users/' && { echo "developer-local dylib path: $binary" >&2; exit 1; }
  list_rpaths "$binary" | grep -q '^/' && { echo "absolute rpath: $binary" >&2; exit 1; }
done
list_rpaths "$STAGING/qwen3-asr-cli" | grep -Fxq '@executable_path' \
  || { echo "missing @executable_path rpath" >&2; exit 1; }
"$STAGING/qwen3-asr-cli" --help >/dev/null 2>&1 \
  || { echo "qwen3-asr-cli failed to launch from staged runtime" >&2; exit 1; }

rm -rf "$DEST_BIN"
mv "$STAGING" "$DEST_BIN"
STAGING=""
echo "Qwen aligner runtime staged: $DEST_BIN"
