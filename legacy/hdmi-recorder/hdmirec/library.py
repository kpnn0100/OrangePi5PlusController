"""Recordings library for the gallery: finds recordings and tells what each file is.

Files of one take share a base name (REC_20260927_021350): the .arh raw file and its
_H265.mov / _FFV1.mkv copies, or a live-recorded .mp4/.mkv. Each file is classified by
its actual content (ARH header, or the codec GStreamer's discoverer finds), not only
its name.
"""
import os
import re
import threading
from datetime import datetime

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
from gi.repository import Gst, GstPbutils  # noqa: E402

from . import arh  # noqa: E402

RAW, H265, FFV1, OTHER = "RAW", "H.265", "FFV1", "VIDEO"
KINDS = (RAW, H265, FFV1)
EXTENSIONS = (".arh", ".mp4", ".mkv", ".mov")
_BASE = re.compile(r"^(.*?)(?:_H265|_FFV1)*\.(?:arh|mp4|mkv|mov)$", re.I)


class Item:
    """One playable file."""

    def __init__(self, path):
        self.path = path
        st = os.stat(path)
        self.size, self.mtime = st.st_size, st.st_mtime
        self.kind = OTHER
        self.width = self.height = 0
        self.fps = 0.0
        self.duration = 0.0            # seconds
        self.format = ""               # pixel format (RAW) or codec detail
        self.audio = False
        self.problem = None            # e.g. "damaged: no index" / "still recording"

    @property
    def name(self):
        return os.path.basename(self.path)

    def summary(self):
        res = ""
        if self.width:
            res = {2160: "4K", 1080: "1080p", 720: "720p"}.get(self.height, f"{self.height}p")
            res += f"{self.fps:.2f}".rstrip("0").rstrip(".") if self.fps else ""
        parts = [p for p in (res, self.format, _dur(self.duration) if self.duration else "",
                             _size(self.size)) if p]
        return " · ".join(parts)


class Clip:
    """One take: all files sharing a base name."""

    def __init__(self, base):
        self.base = base
        self.items = []

    @property
    def title(self):
        m = re.search(r"(\d{8})_(\d{6})", self.base)
        if m:
            try:
                return datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").strftime(
                    "%d %b %Y  %H:%M:%S")
            except ValueError:
                pass
        return os.path.basename(self.base)

    @property
    def newest(self):
        return max(i.mtime for i in self.items)

    def kinds(self):
        return {i.kind for i in self.items}


def _dur(s):
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _size(n):
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n / 1e3:.0f} kB"


def _probe_arh(item):
    r = arh.ArhReader(item.path)
    v = r.video
    item.kind = RAW
    item.width, item.height, item.fps = v["width"], v["height"], r.fps
    item.format = v["format"]
    item.audio = bool(r.audio)
    if r.finalized:
        item.duration = r.duration / 1e9
    else:
        item.duration = r.estimate_frames() / r.fps if r.fps else 0
        item.problem = "not finalized (still recording, or interrupted)"


def _probe_media(item, discoverer):
    try:
        info = discoverer.discover_uri(Gst.filename_to_uri(item.path))
    except Exception as e:  # GLib.Error: unreadable / damaged file
        item.problem = f"can't be read ({getattr(e, 'message', e)})"
        item.kind = H265 if "_H265" in item.name or item.name.endswith(".mp4") else \
            FFV1 if "_FFV1" in item.name else OTHER
        return
    item.duration = info.get_duration() / Gst.SECOND if info.get_duration() else 0
    for s in info.get_video_streams():
        caps = s.get_caps().to_string() if s.get_caps() else ""
        item.width, item.height = s.get_width(), s.get_height()
        if s.get_framerate_denom():
            item.fps = s.get_framerate_num() / s.get_framerate_denom()
        if "video/x-h265" in caps:
            item.kind = H265
        elif "video/x-ffv" in caps:
            item.kind, item.format = FFV1, "lossless"
        break
    item.audio = bool(info.get_audio_streams())
    if not info.get_video_streams():
        item.problem = "no video stream"


class Library:
    """Scans a folder in a background thread; results are cached per file."""

    def __init__(self):
        self._cache = {}               # path -> (mtime, size, Item)
        self._lock = threading.Lock()

    def scan(self, folder, on_done):
        """Scan folder; on_done(list_of_clips_newest_first) is called from the thread."""
        threading.Thread(target=self._scan, args=(folder, on_done), daemon=True).start()

    def _scan(self, folder, on_done):
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().endswith(EXTENSIONS) and not n.endswith(".part"))
        except OSError:
            on_done([])
            return
        discoverer = None
        clips = {}
        for n in names:
            path = os.path.join(folder, n)
            try:
                st = os.stat(path)
            except OSError:
                continue
            with self._lock:
                cached = self._cache.get(path)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                item = cached[2]
            else:
                item = Item(path)
                try:
                    if n.lower().endswith(".arh"):
                        _probe_arh(item)
                    else:
                        if discoverer is None:
                            discoverer = GstPbutils.Discoverer.new(3 * Gst.SECOND)
                        _probe_media(item, discoverer)
                except (OSError, ValueError) as e:
                    item.problem = f"can't be read ({e})"
                with self._lock:
                    self._cache[path] = (st.st_mtime, st.st_size, item)
            m = _BASE.match(n)
            base = os.path.join(folder, m.group(1) if m else n)
            clips.setdefault(base, Clip(base)).items.append(item)
        order = {RAW: 0, H265: 1, FFV1: 2, OTHER: 3}
        for c in clips.values():
            c.items.sort(key=lambda i: order.get(i.kind, 9))
        on_done(sorted(clips.values(), key=lambda c: c.newest, reverse=True))
