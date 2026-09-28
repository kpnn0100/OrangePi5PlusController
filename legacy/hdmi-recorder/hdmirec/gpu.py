"""GPU (Vulkan) FFV1 encoding, installed by setup_gpu.sh.

The Mali-G610 driver shipped with the OS (g13p0) has no Vulkan; setup_gpu.sh puts
Rockchip's g24p0 driver (Vulkan 1.3) and an FFmpeg 9 build with the ffv1_vulkan encoder
in ~/.local/share/hdmi-recorder/gpu. Only the encoder process loads that driver, through
VK_ICD_FILENAMES; the desktop keeps its own.

Measured on 4K 4:2:2 camera footage: GPU 5 fps using 0.2 CPU cores, CPU FFV1 14 fps using
6.3 cores - the GPU is slower but leaves the CPU (and power budget) to the capture.
Both produce identical, bit-exact output.
"""
import os

GPU_DIR = os.environ.get("HDMIREC_GPU_DIR",
                         os.path.expanduser("~/.local/share/hdmi-recorder/gpu"))


def ffmpeg():
    """(ffmpeg path, environment) for GPU encoding, or None if setup_gpu.sh wasn't run."""
    exe = os.path.join(GPU_DIR, "ffmpeg", "bin", "ffmpeg")
    icd = os.path.join(GPU_DIR, "mali_icd.json")
    if not (os.access(exe, os.X_OK) and os.path.exists(icd)):
        return None
    env = dict(os.environ, VK_ICD_FILENAMES=icd)
    return exe, env


def available():
    return ffmpeg() is not None


# pixel formats the recorder captures -> planar format FFV1 stores (lossless re-layout)
PLANAR = {"NV12": "yuv420p", "NV21": "yuv420p", "NV16": "yuv422p", "NV61": "yuv422p",
          "NV24": "yuv444p", "YUY2": "yuv422p", "UYVY": "yuv422p", "BGR": "gbrp", "RGB": "gbrp"}
FFMPEG_PIXFMT = {"NV12": "nv12", "NV21": "nv21", "NV16": "nv16", "NV61": "nv61", "NV24": "nv24",
                 "YUY2": "yuyv422", "UYVY": "uyvy422", "BGR": "bgr24", "RGB": "rgb24"}
