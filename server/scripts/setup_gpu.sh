#!/usr/bin/env bash
# GPU (Vulkan) FFV1 encoding for the recorder. No root needed, nothing system-wide changes:
#   - Rockchip's Mali-G610 driver g24p0, which includes Vulkan 1.3 (the installed g13p0
#     has none). It is used ONLY by the recorder's encoder, through a private Vulkan ICD
#     manifest; the desktop keeps its current graphics driver.
#   - FFmpeg 9 (static arm64 build) with the ffv1_vulkan encoder.
# Everything goes to ~/.local/share/hdmi-recorder/gpu. Remove that folder to undo.
set -euo pipefail

DEST=${HDMIREC_GPU_DIR:-$HOME/.local/share/hdmi-recorder/gpu}
LIBMALI_URL=https://raw.githubusercontent.com/JeffyCN/mirrors/libmali/lib/aarch64-linux-gnu/libmali-valhall-g610-g24p0-x11-gbm.so
FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-linuxarm64-gpl-9.0.tar.xz
OLD=$HOME/gpu      # earlier manual download, reused if present

mkdir -p "$DEST"
cd "$DEST"

if [ ! -f libmali-g24p0-x11-gbm.so ]; then
    if [ -f "$OLD/libmali-g24p0-x11-gbm.so" ]; then
        mv "$OLD/libmali-g24p0-x11-gbm.so" .
    else
        echo "Downloading Mali-G610 driver g24p0 (58 MB)…"
        curl -fL --retry 3 -o libmali-g24p0-x11-gbm.so.tmp "$LIBMALI_URL"
        mv libmali-g24p0-x11-gbm.so.tmp libmali-g24p0-x11-gbm.so
    fi
fi

if [ ! -x ffmpeg/bin/ffmpeg ]; then
    if [ -x "$OLD/ffmpeg9/bin/ffmpeg" ]; then
        mv "$OLD/ffmpeg9" ffmpeg
    else
        echo "Downloading FFmpeg 9 (arm64, 127 MB)…"
        curl -fL --retry 3 -o ffmpeg.tar.xz "$FFMPEG_URL"
        tar xf ffmpeg.tar.xz && rm ffmpeg.tar.xz
        mv ffmpeg-n9.0* ffmpeg
    fi
fi

cat > mali_icd.json <<JSON
{
  "file_format_version" : "1.0.0",
  "ICD" : {
    "library_path" : "$DEST/libmali-g24p0-x11-gbm.so",
    "api_version" : "1.3.276"
  }
}
JSON

rmdir "$OLD/ffmpeg9" 2>/dev/null || true
rm -f "$OLD/mali_icd.json" "$OLD/bench_gpu.sh"
rmdir "$OLD" 2>/dev/null || true

echo "Testing a GPU FFV1 encode…"
if VK_ICD_FILENAMES=$DEST/mali_icd.json ffmpeg/bin/ffmpeg -v error -y \
        -init_hw_device vulkan=vk:0 -filter_hw_device vk \
        -f lavfi -i testsrc2=size=1280x720:rate=30 -frames:v 10 \
        -vf format=yuv422p,hwupload -c:v ffv1_vulkan -f null - 2>/tmp/hdmirec-gpu-test.log; then
    echo "OK: GPU FFV1 is available to the recorder ($DEST)."
else
    grep -v arm_release_ver /tmp/hdmirec-gpu-test.log >&2 || true
    echo "GPU FFV1 test failed; the recorder keeps using the CPU." >&2
    exit 1
fi
