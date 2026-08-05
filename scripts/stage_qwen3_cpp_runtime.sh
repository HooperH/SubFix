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

for binary in "$STAGING/qwen3-asr-cli" "$STAGING"/*.dylib; do
  file "$binary" | grep -q arm64 || { echo "non-arm64 binary: $binary" >&2; exit 1; }
  otool -L "$binary" | tail -n +2 | grep -q '/Users/' && { echo "developer-local dylib path: $binary" >&2; exit 1; }
done

rm -rf "$DEST_BIN"
mv "$STAGING" "$DEST_BIN"
STAGING=""
echo "Qwen aligner runtime staged: $DEST_BIN"
