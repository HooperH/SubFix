#!/bin/bash
set -euo pipefail

: "${PYTHON_RUNTIME_URL:?missing PYTHON_RUNTIME_URL}"
: "${PYTHON_RUNTIME_SHA256:?missing PYTHON_RUNTIME_SHA256}"

OUTPUT_DIR="${1:?usage: fetch_python_runtime.sh OUTPUT_DIR}"
if [[ "$PYTHON_RUNTIME_URL" != https://* ]]; then
  echo "PYTHON_RUNTIME_URL must use HTTPS" >&2
  exit 2
fi
if [[ ! "$PYTHON_RUNTIME_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "PYTHON_RUNTIME_SHA256 must contain 64 hexadecimal characters" >&2
  exit 2
fi

OUTPUT_PARENT="$(dirname "$OUTPUT_DIR")"
OUTPUT_NAME="$(basename "$OUTPUT_DIR")"
ARCHIVE="${OUTPUT_DIR}.archive.part"
mkdir -p "$OUTPUT_PARENT"

curl --fail --location --continue-at - "$PYTHON_RUNTIME_URL" --output "$ARCHIVE"
echo "${PYTHON_RUNTIME_SHA256}  ${ARCHIVE}" | shasum -a 256 -c -

STAGING="$(mktemp -d "${OUTPUT_PARENT}/.${OUTPUT_NAME}.staging.XXXXXX")"
cleanup() {
  if [[ -n "${STAGING:-}" && -d "$STAGING" ]]; then
    rm -rf "$STAGING"
  fi
  return 0
}
trap cleanup EXIT

tar --extract --file "$ARCHIVE" --directory "$STAGING" --strip-components=1
RUNTIME_PYTHON="$STAGING/bin/python3"
[[ -x "$RUNTIME_PYTHON" ]] || { echo "downloaded runtime does not contain bin/python3" >&2; exit 1; }
"$RUNTIME_PYTHON" -c 'import platform,sys; assert platform.machine()=="arm64"; assert sys.version_info[:2]==(3,12)'

rm -rf "$OUTPUT_DIR"
mv "$STAGING" "$OUTPUT_DIR"
STAGING=""
echo "Python runtime installed: $OUTPUT_DIR"
