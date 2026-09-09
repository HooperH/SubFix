#!/bin/bash
# Build the FFmpeg binary distributed with the Apple-Silicon SubFix package.
set -euo pipefail

FFMPEG_VERSION="9.0"
FFMPEG_ARCHIVE="ffmpeg-9.0.tar.xz"
FFMPEG_SHA256="7f607a00dd0d28a729d5a4811205812eef01cf6ef6155025febb6f36a9062d52"
SOURCE_URL="https://ffmpeg.org/releases/${FFMPEG_ARCHIVE}"
OUTPUT_DIR="${1:-}"
SOURCE_ARCHIVE="${FFMPEG_SOURCE_ARCHIVE:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  echo "用法: $0 <输出目录>" >&2
  exit 2
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "内置 FFmpeg 只能在 Apple Silicon（arm64）构建机上构建。" >&2
  exit 1
fi
for required in curl shasum tar make clang file; do
  command -v "$required" >/dev/null || { echo "缺少构建工具：$required" >&2; exit 1; }
done

mkdir -p "$OUTPUT_DIR"
WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

echo "⬇️ 下载并校验 FFmpeg ${FFMPEG_VERSION} 官方源码…"
if [[ -n "$SOURCE_ARCHIVE" ]]; then
  [[ -f "$SOURCE_ARCHIVE" ]] || { echo "指定的 FFmpeg 源码缓存不存在：$SOURCE_ARCHIVE" >&2; exit 1; }
  cp "$SOURCE_ARCHIVE" "$WORK_DIR/$FFMPEG_ARCHIVE"
else
  curl --fail --location --proto '=https' --tlsv1.2 --output "$WORK_DIR/$FFMPEG_ARCHIVE" "$SOURCE_URL"
fi
ACTUAL_SHA256="$(shasum -a 256 "$WORK_DIR/$FFMPEG_ARCHIVE" | awk '{print $1}')"
[[ "$ACTUAL_SHA256" == "$FFMPEG_SHA256" ]] || {
  echo "FFmpeg 源码 SHA-256 不匹配：$ACTUAL_SHA256" >&2
  exit 1
}

tar -xf "$WORK_DIR/$FFMPEG_ARCHIVE" -C "$WORK_DIR"
SOURCE_DIR="$WORK_DIR/ffmpeg-$FFMPEG_VERSION"
[[ -d "$SOURCE_DIR" ]] || { echo "FFmpeg 源码目录异常。" >&2; exit 1; }

echo "🔨 构建 arm64 LGPL FFmpeg…"
(
  cd "$SOURCE_DIR"
  ./configure \
    --arch=arm64 \
    --target-os=darwin \
    --cc=clang \
    --disable-gpl \
    --disable-nonfree \
    --disable-debug \
    --disable-doc \
    --disable-autodetect \
    --disable-everything \
    --disable-network \
    --disable-avdevice \
    --enable-videotoolbox \
    --enable-static \
    --disable-shared \
    --enable-ffmpeg \
    --enable-protocol=file \
    --enable-demuxer=mov,matroska,mxf,wav,mp3,aac,avi,mpegts,flv \
    --enable-decoder=aac,ac3,eac3,alac,flac,mp3,opus,pcm_alaw,pcm_mulaw,pcm_s16be,pcm_s16le,pcm_s24be,pcm_s24le,pcm_s32be,pcm_s32le,pcm_f32be,pcm_f32le,pcm_f64be,pcm_f64le,vorbis \
    --enable-parser=aac,ac3,mpegaudio,opus,vorbis \
    --enable-encoder=pcm_s16le \
    --enable-muxer=wav \
    --enable-filter=aresample,pan
  make -j"$(sysctl -n hw.ncpu)" ffmpeg
)

install -m 755 "$SOURCE_DIR/ffmpeg" "$OUTPUT_DIR/ffmpeg"
cp "$SOURCE_DIR/COPYING.LGPLv2.1" "$OUTPUT_DIR/FFmpeg-LGPL-2.1.txt"
cp "$WORK_DIR/$FFMPEG_ARCHIVE" "$OUTPUT_DIR/$FFMPEG_ARCHIVE"
cat > "$OUTPUT_DIR/FFmpeg-BUILD-INFO.txt" <<EOF
FFmpeg version: $FFMPEG_VERSION
Source: $SOURCE_URL
SHA-256: $FFMPEG_SHA256
Configure: --arch=arm64 --target-os=darwin --cc=clang --disable-gpl --disable-nonfree --disable-debug --disable-doc --disable-autodetect --disable-everything --disable-network --disable-avdevice --enable-videotoolbox --enable-static --disable-shared --enable-ffmpeg --enable-protocol=file --enable-demuxer=mov,matroska,mxf,wav,mp3,aac,avi,mpegts,flv --enable-decoder=aac,ac3,eac3,alac,flac,mp3,opus,pcm_alaw,pcm_mulaw,pcm_s16be,pcm_s16le,pcm_s24be,pcm_s24le,pcm_s32be,pcm_s32le,pcm_f32be,pcm_f32le,pcm_f64be,pcm_f64le,vorbis --enable-parser=aac,ac3,mpegaudio,opus,vorbis --enable-encoder=pcm_s16le --enable-muxer=wav --enable-filter=aresample,pan
EOF

file "$OUTPUT_DIR/ffmpeg"
file "$OUTPUT_DIR/ffmpeg" | grep -q "arm64" || { echo "构建产物不是 arm64。" >&2; exit 1; }
"$OUTPUT_DIR/ffmpeg" -version >/dev/null
echo "✅ 内置 FFmpeg 已生成：$OUTPUT_DIR/ffmpeg"
