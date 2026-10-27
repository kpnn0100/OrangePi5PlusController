"""Recorder settings (REC-04): ~/.config/arstro-remote/recorder.json.

Shared by all controllers - every change goes through `apply_patch`, which validates
it, saves the file and returns the new settings for the hub to publish.
On first start the settings of the standalone hdmi-recorder app are imported if present.
"""

import copy
import json
import os
import threading

_lock = threading.Lock()

DEFAULTS = {
    "mode": "h265",                 # h265 (real-time VPU) | raw (.arh)
    "h265": {
        "bitrate": 80,              # Mbit/s
        "rc": "cbr",                # cbr | vbr
        "gop": 1.0,                 # keyframe interval, seconds
        "container": "mov",         # mov (PCM audio, editors) | mkv (PCM) | mp4 (AAC audio, plays everywhere)
    },
    "raw": {
        # extra copies of a RAW take (REC-04), made during or after recording - H.264, H.265, FFV1
        "h264": False,              # also make an H.264 MP4 share copy (VPU)
        "h264_quality": "high",     # high | higher | max
        "h264_scale": "source",     # source | 1080 | 720
        "hq": False,                # also make a high-quality H.265 file
        "hq_engine": "vpu",         # vpu (hardware, ~real time) | x265 (CPU, best, slow)
        "hq_quality": "high",       # high | higher | max
        "x265_preset": "fast",
        "hq_chroma": "420",         # 420 (compatible) | source (keep 4:2:2 / 4:4:4)
        "ffv1": False,              # also make a lossless FFV1 file
        "ffv1_engine": "cpu",       # cpu (faster) | gpu (Vulkan: slower, ~0 CPU)
        "when": "during",           # during | after : when the encodes start
        "ffv1_replace_raw": True,   # check the FFV1 copy byte for byte, then delete the RAW (GAL-08)
    },
    "audio": {"record": True,
              "source": "hdmi"},    # hdmi | alsa:<card id> (a USB audio interface, REC-09)
    # on-set monitoring, shared by every viewer (REC-13, REC-14)
    "monitor": {
        "guides": "off",            # off | 2.39 | 1.85 | 16:9 | 4:3 | 1:1 | 9:16
        "safe": False,              # action (93 %) and title (90 %) safe areas
        "center": False,            # centre cross
        "zebra": False,
        "zebra_level": 95,          # IRE %
        "false_color": False,
        "scope": "off",             # off | histogram | waveform
        "meters": True,             # audio level meters
        "lut": "off",               # off | a built-in (slog3, vlog, hlg, log) | cube:<name>
        "zoom": 3,                  # focus zoom: times the whole picture (3 = 1:1 pixels for 4K)
    },
    # the slate: names the next take, counted up after each one (REC-15)
    "slate": {"scene": "", "shot": "", "take": 1, "auto": True},
    # a record button and a tally light on GPIO lines, "gpiochipN:LINE" or "" (REC-16)
    "gpio": {"button": "", "button_active_low": True, "tally": "", "tally_active_low": False},
    "storage_guard": True,          # refuse a take the measured disk cannot keep up with (REC-12)
    "storage": "~/Videos/HDMI-Recorder",
    "edid": "4k60",                 # 4k60 | 4k30 | 1080p | keep
    "preview": {"quality": "medium"},   # low | medium | high
}

# key path -> allowed values (tuple) or (type, min, max)
SCHEMA = {
    ("mode",): ("h265", "raw"),
    ("h265", "bitrate"): (int, 2, 200),
    ("h265", "rc"): ("cbr", "vbr"),
    ("h265", "gop"): (float, 0.1, 10.0),
    ("h265", "container"): ("mov", "mkv", "mp4"),
    ("raw", "h264"): (bool,),
    ("raw", "h264_quality"): ("high", "higher", "max"),
    ("raw", "h264_scale"): ("source", "1080", "720"),
    ("raw", "hq"): (bool,),
    ("raw", "hq_engine"): ("vpu", "x265"),
    ("raw", "hq_quality"): ("high", "higher", "max"),
    ("raw", "x265_preset"): ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
    ("raw", "hq_chroma"): ("420", "source"),
    ("raw", "ffv1"): (bool,),
    ("raw", "ffv1_engine"): ("cpu", "gpu"),
    ("raw", "when"): ("during", "after"),
    ("raw", "ffv1_replace_raw"): (bool,),
    ("audio", "record"): (bool,),
    ("audio", "source"): lambda v: _match(v, r"^(hdmi|alsa:[A-Za-z0-9_-]{1,32})$",
                                         "hdmi or alsa:<card id>"),
    ("monitor", "guides"): ("off", "2.39", "1.85", "16:9", "4:3", "1:1", "9:16"),
    ("monitor", "safe"): (bool,),
    ("monitor", "center"): (bool,),
    ("monitor", "zebra"): (bool,),
    ("monitor", "zebra_level"): (int, 50, 109),
    ("monitor", "false_color"): (bool,),
    ("monitor", "scope"): ("off", "histogram", "waveform"),
    ("monitor", "meters"): (bool,),
    ("monitor", "lut"): lambda v: _match(v, r"^(off|slog3|vlog|hlg|log|cube:[A-Za-z0-9][A-Za-z0-9 ._+-]{0,59})$",
                                         "off, slog3, vlog, hlg, log or cube:<name>"),
    ("monitor", "zoom"): (2, 3, 4, 6, 8),
    ("slate", "scene"): lambda v: _match(v, r"^[A-Za-z0-9-]{0,16}$", "up to 16 letters, digits or -"),
    ("slate", "shot"): lambda v: _match(v, r"^[A-Za-z0-9-]{0,16}$", "up to 16 letters, digits or -"),
    ("slate", "take"): (int, 1, 999),
    ("slate", "auto"): (bool,),
    ("gpio", "button"): lambda v: _match(v, r"^((gpiochip)?\d+:\d{1,3})?$", "chip:line, e.g. gpiochip1:5, or empty"),
    ("gpio", "button_active_low"): (bool,),
    ("gpio", "tally"): lambda v: _match(v, r"^((gpiochip)?\d+:\d{1,3})?$", "chip:line, e.g. gpiochip1:5, or empty"),
    ("gpio", "tally_active_low"): (bool,),
    ("storage_guard",): (bool,),
    ("storage",): (str,),
    ("edid",): ("4k60", "4k30", "1080p", "keep"),
    ("preview", "quality"): ("low", "medium", "high"),
}


def path():
    from ..paths import config_dir
    return os.path.join(config_dir(), "recorder.json")


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif k in base:
            base[k] = v
    return base


def _legacy():
    p = os.path.expanduser("~/.config/hdmi-recorder/settings.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def storage_dir(s):
    return os.path.abspath(os.path.expanduser(s["storage"]))


def load():
    s = copy.deepcopy(DEFAULTS)
    with _lock:
        try:
            with open(path()) as f:
                _merge(s, json.load(f))
        except FileNotFoundError:
            old = _legacy()
            if old:
                old.pop("edid_name", None)
                _merge(s, old)
                _save(s)
        except (OSError, ValueError):
            pass
    return s


def _save(s):
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, p)


def _flatten(d, prefix=()):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flatten(v, prefix + (k,))
        else:
            yield prefix + (k,), v


def _match(value, pattern, what):
    import re
    v = "" if value is None else str(value).strip()
    if not re.match(pattern, v):
        raise ValueError("must be %s" % what)
    return v


def _coerce(key, value):
    rule = SCHEMA.get(key)
    if rule is None:
        raise ValueError("unknown setting %s" % ".".join(key))
    if callable(rule):
        try:
            return rule(value)
        except ValueError as e:
            raise ValueError("%s %s" % (".".join(key), e))
    if rule and isinstance(rule[0], type):
        typ = rule[0]
        if typ is bool:
            if isinstance(value, str):
                if value.lower() in ("1", "true", "yes", "on"):
                    return True
                if value.lower() in ("0", "false", "no", "off"):
                    return False
                raise ValueError("%s must be true or false" % ".".join(key))
            return bool(value)
        try:
            v = typ(value)
        except (TypeError, ValueError):
            raise ValueError("%s must be a %s" % (".".join(key), typ.__name__))
        if len(rule) == 3 and not rule[1] <= v <= rule[2]:
            raise ValueError("%s must be between %s and %s" % (".".join(key), rule[1], rule[2]))
        if typ is str and not v.strip():
            raise ValueError("%s must not be empty" % ".".join(key))
        return v
    if value not in rule and isinstance(value, str) and all(isinstance(x, int) for x in rule):
        try:
            value = int(value)
        except ValueError:
            pass
    if value not in rule:
        raise ValueError("%s must be one of %s" % (".".join(key), ", ".join(map(str, rule))))
    return value


def parse_assignment(text):
    """'h265.bitrate=60' -> {"h265": {"bitrate": "60"}} (CLI helper)."""
    key, sep, value = text.partition("=")
    if not sep:
        raise ValueError("expected KEY=VALUE, e.g. h265.bitrate=60")
    parts = key.strip().split(".")
    out = cur = {}
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value.strip()
    return out


def apply_patch(patch):
    """Validate and apply a (nested) partial settings dict; returns the new settings."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("settings patch must be a non-empty object")
    s = load()
    for key, value in _flatten(patch):
        v = _coerce(key, value)
        cur = s
        for k in key[:-1]:
            cur = cur[k]
        cur[key[-1]] = v
    if "storage" in patch:
        folder = storage_dir(s)
        os.makedirs(folder, exist_ok=True)
        if not os.access(folder, os.W_OK):
            raise ValueError("storage folder %s is not writable" % folder)
    with _lock:
        _save(s)
    return s
