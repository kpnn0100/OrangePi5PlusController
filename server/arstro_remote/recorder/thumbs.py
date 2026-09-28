"""Take thumbnails (GAL-02): one 480 px JPEG per take in ~/.cache/arstro-remote/thumbs.

Made on first request from the best file of the take (encoded copies decode fastest;
a RAW .arh is read directly: its first frame is handed to ffmpeg as rawvideo).
"""

import os
import subprocess
import threading

from . import arh, gpu

_locks = {}
_guard = threading.Lock()
WIDTH = 480


def thumb_dir():
    from ..paths import cache_dir
    d = os.path.join(cache_dir(), "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def _lock_for(key):
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _from_media(src, out, duration):
    t = min(1.0, max(0.0, (duration or 0) / 3))
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", "%.2f" % t, "-i", src, "-frames:v", "1",
           "-vf", "scale=%d:-2" % WIDTH, "-q:v", "4", out]
    return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0


def _from_arh(src, out):
    r = arh.ArhReader(src)
    v = r.video
    pix = gpu.FFMPEG_PIXFMT.get(v["format"])
    if not pix:
        return False
    frame = next(r.chunks((arh.VFRM,), False), None)
    if not frame:
        return False
    data = frame[3]
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", pix,
           "-s", "%dx%d" % (v["width"], v["height"]), "-i", "pipe:0", "-frames:v", "1",
           "-vf", "scale=%d:-2" % WIDTH, "-q:v", "4", out]
    return subprocess.run(cmd, input=bytes(data[:v["frame_size"]]), capture_output=True,
                          timeout=30).returncode == 0


def get(folder, take):
    """Path of the take's thumbnail (made if needed), or None."""
    items = take["items"]
    usable = [i for i in items if not i.get("recording") and not str(i.get("problem") or "").startswith("can't")]
    if not usable:
        return None
    usable.sort(key=lambda i: {"H.264": 0, "H.265": 1, "FFV1": 2, "VIDEO": 3, "RAW": 4}.get(i["kind"], 9))
    out = os.path.join(thumb_dir(), take["id"] + ".jpg")
    newest = max(i["mtime"] for i in usable)
    with _lock_for(take["id"]):
        try:
            if os.path.getmtime(out) >= newest:
                return out
        except OSError:
            pass
        tmp = out + ".tmp.jpg"
        for item in usable:
            src = os.path.join(folder, item["id"])
            try:
                ok = _from_arh(src, tmp) if item["kind"] == "RAW" else \
                    _from_media(src, tmp, item.get("duration"))
            except (OSError, ValueError, subprocess.SubprocessError):
                ok = False
            if ok and os.path.exists(tmp):
                os.replace(tmp, out)
                return out
    return None


def drop(take_id):
    try:
        os.remove(os.path.join(thumb_dir(), take_id + ".jpg"))
    except OSError:
        pass
