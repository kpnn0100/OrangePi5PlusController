"""Recordings library for the gallery (GAL-01).

Files of one take share a base name (REC_20260927_021350): the .arh raw file and its
_H265.mov / _H264.mp4 / _FFV1.mkv copies, or a live-recorded .mp4/.mkv. Each file is
classified by its content (ARH header, or the codec ffprobe reports), not only its name.
Probing runs ffprobe in a subprocess, so the server process never loads GStreamer.
"""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime

from . import arh

RAW, H265, H264, FFV1, OTHER = "RAW", "H.265", "H.264", "FFV1", "VIDEO"
KINDS = (RAW, H265, H264, FFV1, OTHER)
EXTENSIONS = (".arh", ".mp4", ".mkv", ".mov")
_BASE = re.compile(r"^(.*?)(?:_(?:H265|FFV1|H264)(?:_\d+p)?)*\.(?:arh|mp4|mkv|mov)$", re.I)
_STAMP = re.compile(r"(\d{8})_(\d{6})")
ORDER = {RAW: 0, H265: 1, H264: 2, FFV1: 3, OTHER: 4}


def clip_id(filename):
    m = _BASE.match(filename)
    return m.group(1) if m else os.path.splitext(filename)[0]


def _probe_arh(path, item):
    r = arh.ArhReader(path)
    v = r.video
    item.update(kind=RAW, codec="raw", width=v["width"], height=v["height"], fps=round(r.fps, 3),
                pixel_format=v["format"], audio=bool(r.audio))
    if r.finalized:
        item["duration"] = r.duration / 1e9
    else:
        item["duration"] = r.estimate_frames() / r.fps if r.fps else 0
        item["problem"] = "not finalized (still recording, or interrupted)"


def _probe_media(path, item):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                              "-show_streams", path], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        item["problem"] = "ffprobe missing (install ffmpeg)"
        return
    except subprocess.TimeoutExpired:
        item["problem"] = "can't be read (probe timed out)"
        return
    try:
        info = json.loads(out.stdout or "{}")
    except ValueError:
        info = {}
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        item["problem"] = "can't be read" if out.returncode else "no video stream"
        return
    codec = v.get("codec_name", "")
    item["codec"] = codec
    item["kind"] = {"hevc": H265, "h264": H264, "ffv1": FFV1}.get(codec, OTHER)
    item["width"], item["height"] = int(v.get("width") or 0), int(v.get("height") or 0)
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    try:
        n, d = rate.split("/")
        item["fps"] = round(int(n) / int(d), 3) if int(d) else 0.0
    except ValueError:
        pass
    item["pixel_format"] = v.get("pix_fmt", "")
    try:
        item["duration"] = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    except ValueError:
        pass
    item["audio"] = any(s.get("codec_type") == "audio" for s in streams)


def _verified(path):
    """For an FFV1 file: the lossless check against its RAW (GAL-08), or None."""
    try:
        with open(path + ".verified.json") as f:
            v = json.load(f)
        return {"raw": v.get("raw"), "frames": v.get("frames"), "checked": v.get("checked")}
    except (OSError, ValueError):
        return None


def _title(base):
    m = _STAMP.search(base)
    if m:
        try:
            return datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").strftime("%d %b %Y  %H:%M:%S")
        except ValueError:
            pass
    return base


def _created(base, fallback):
    m = _STAMP.search(base)
    if m:
        try:
            return int(datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").timestamp())
        except ValueError:
            pass
    return int(fallback)


class Library:
    """Scans the storage folder; results are cached per file (path, mtime, size)."""

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def scan(self, folder, recording=None, busy=()):
        """-> list of take dicts, newest first. `recording`: path being recorded now,
        `busy`: paths a job reads or writes."""
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().endswith(EXTENSIONS) and not n.startswith("."))
        except OSError:
            return []
        takes = {}
        for n in names:
            p = os.path.join(folder, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            live = recording is not None and os.path.abspath(p) == os.path.abspath(recording)
            with self._lock:
                cached = self._cache.get(p)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size and not live:
                item = dict(cached[2])
            else:
                item = {"id": n, "kind": OTHER, "codec": "", "size": st.st_size, "width": 0,
                        "height": 0, "fps": 0.0, "duration": 0.0, "audio": False, "pixel_format": "",
                        "problem": None, "mtime": int(st.st_mtime)}
                if not live:
                    try:
                        if n.lower().endswith(".arh"):
                            _probe_arh(p, item)
                        else:
                            _probe_media(p, item)
                    except (OSError, ValueError, KeyError) as e:
                        item["problem"] = "can't be read (%s)" % e
                    with self._lock:
                        self._cache[p] = (st.st_mtime, st.st_size, dict(item))
                else:
                    item["kind"] = RAW if n.lower().endswith(".arh") else H265
            item["recording"] = live
            item["busy"] = live or os.path.abspath(p) in busy
            item["verified"] = _verified(p) if item["kind"] == FFV1 else None
            if live:
                item["problem"] = "recording…"
            item["url"] = "/api/media/" + n
            base = clip_id(n)
            takes.setdefault(base, []).append(item)
        out = []
        for base, items in takes.items():
            items.sort(key=lambda i: ORDER.get(i["kind"], 9))
            newest = max(i["mtime"] for i in items)
            out.append({
                "id": base, "title": _title(base), "created": _created(base, newest),
                "size": sum(i["size"] for i in items),
                "duration": max((i["duration"] or 0) for i in items),
                "kinds": sorted({i["kind"] for i in items}, key=lambda k: ORDER.get(k, 9)),
                "recording": any(i["recording"] for i in items),
                "items": items, "thumb": "/api/thumb/" + base,
            })
        out.sort(key=lambda c: (c["created"], c["id"]), reverse=True)
        return out

    def forget(self, path):
        with self._lock:
            self._cache.pop(path, None)


def fmt_size(n):
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n / 1e3:.0f} kB"


def now():
    return time.time()
