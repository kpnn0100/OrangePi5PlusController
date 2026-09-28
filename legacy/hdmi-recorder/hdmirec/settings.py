"""Persistent user settings (~/.config/hdmi-recorder/settings.json)."""
import copy
import json
import os

PATH = os.path.expanduser("~/.config/hdmi-recorder/settings.json")

DEFAULTS = {
    "mode": "h265",                 # h265 (real-time VPU) | raw (.arh)
    "h265": {
        "bitrate": 80,              # Mbit/s
        "rc": "cbr",                # cbr | vbr
        "gop": 1.0,                 # keyframe interval, seconds
        "container": "mp4",         # mp4 | mkv
    },
    "raw": {
        "hq": False,                # also make a high-quality H.265 file
        "hq_engine": "vpu",         # vpu (hardware, ~real time) | x265 (CPU, best, slow)
        "hq_quality": "high",       # high | higher | max
        "x265_preset": "fast",
        "hq_chroma": "420",         # 420 (compatible) | source (keep 4:2:2 / 4:4:4)
        "ffv1": False,              # also make a lossless FFV1 file
        "ffv1_engine": "cpu",       # cpu (faster) | gpu (Vulkan: slower, ~0 CPU)
        "when": "during",           # during | after : when the encodes start
    },
    "audio": {"record": True, "monitor": True},
    "storage": os.path.expanduser("~/Videos/HDMI-Recorder"),
    "edid": "4k60",                 # 4k60 | 4k30 | 1080p | keep | file:<hex EDID copied from a screen>
    "edid_name": "",                # label for a copied EDID
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif k in base:
            base[k] = v
    return base


def load():
    s = copy.deepcopy(DEFAULTS)
    try:
        with open(PATH) as f:
            _merge(s, json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save(s):
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, PATH)
    except OSError as e:
        print(f"[warn] could not save settings: {e}")
