"""What a take is beyond its files (REC-15, GAL-13): slate, timecode, notes, markers, sync.

Each take has a sidecar next to its files, `<take>.take.json`:

    {"version": 1, "take": "S12A_SH3_T04_20261027_153012",
     "slate": {"scene": "12A", "shot": "3", "take": 4},
     "timecode": {"tc": "15:30:12;07", "frames": ..., "rate": "30000/1001", ...},  (timecode.py)
     "clock_synced": true, "good": false, "notes": "",
     "markers": [{"tc": "15:30:20;11", "frames": 244, "t": 8.14, "label": "clap"}],
     "sync": {"audio_s": 61.002, "video_s": 61.027, "drift_ms": -25.0, "audio": "HDMI"},
     "source": "3840×2160p29.97 · NV16", "frames": 1830, "drops": 0}

The gallery shows it, `gallery.annotate` edits it, `gallery.csv` turns it into a sheet an
editor imports (DaVinci Resolve: File > Import > Metadata, matched by "File Name").
"""

import csv
import io
import json
import os
import re
import threading
from datetime import datetime

from . import timecode

SUFFIX = ".take.json"
_lock = threading.Lock()


def clean(text, n=16):
    """A slate field as it may appear in a file name: letters, digits and '-'."""
    return re.sub(r"[^A-Za-z0-9-]", "", str(text or ""))[:n]


def take_name(slate, when=None):
    """REC_20261027_153012, or with a scene: S12A_SH3_T04_20261027_153012."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    scene = clean((slate or {}).get("scene"))
    if not scene:
        return "REC_" + stamp
    shot = clean(slate.get("shot"))
    return "S%s%s_T%02d_%s" % (scene, "_SH" + shot if shot else "", int(slate.get("take") or 1), stamp)


def take_prefix(slate):
    """The part of the next take's name the slate decides: S12A_SH3_T04, or REC."""
    return take_name(slate).rsplit("_", 2)[0]


def path_for(folder, take):
    return os.path.join(folder, take + SUFFIX)


def load(folder, take):
    try:
        with open(path_for(folder, take)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(folder, take, data):
    p = path_for(folder, take)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def update(folder, take, fn):
    """Read-modify-write the sidecar under a lock; fn(dict) changes it in place."""
    with _lock:
        d = load(folder, take) or {"version": 1, "take": take}
        fn(d)
        save(folder, take, d)
        return d


def delete(folder, take):
    try:
        os.remove(path_for(folder, take))
    except OSError:
        pass


def summary(d):
    """What the gallery list shows of a sidecar."""
    if not d:
        return None
    sl = d.get("slate") or {}
    tc = d.get("timecode") or {}
    sync = d.get("sync") or {}
    return {"scene": sl.get("scene") or "", "shot": sl.get("shot") or "", "take": sl.get("take"),
            "tc": tc.get("tc"), "rate": tc.get("rate"), "good": bool(d.get("good")),
            "notes": d.get("notes") or "", "markers": d.get("markers") or [],
            "drift_ms": sync.get("drift_ms"), "audio_source": sync.get("audio"),
            "clock_synced": d.get("clock_synced"), "drops": d.get("drops")}


def slate_title(d):
    sl = (d or {}).get("slate") or {}
    if not sl.get("scene"):
        return None
    parts = ["Scene %s" % sl["scene"]]
    if sl.get("shot"):
        parts.append("Shot %s" % sl["shot"])
    parts.append("Take %s" % sl.get("take", 1))
    return " · ".join(parts)


CSV_COLUMNS = ["File Name", "Clip Directory", "Scene", "Shot", "Take", "Good Take", "Comments",
               "Start TC", "End TC", "Duration TC", "FPS", "Resolution", "Codec", "Audio",
               "Markers", "A/V Sync (ms)", "Dropped Frames", "Date Recorded", "Clock Synced"]


def to_csv(takes, folder):
    """One row per file of the given gallery takes (library.scan's dicts, with `meta`)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for t in takes:
        d = t.get("meta_full") or {}
        sl = d.get("slate") or {}
        tc = d.get("timecode") or {}
        sync = d.get("sync") or {}
        markers = " | ".join(("%s %s" % (m.get("tc", ""), m.get("label", ""))).strip()
                             for m in d.get("markers") or [])
        for it in t["items"]:
            frames = round((it.get("duration") or 0) * (it.get("fps") or 0))
            start = tc.get("tc", "")
            end = timecode.offset_label(tc, frames) if tc.get("rate") and frames else ""
            dur = timecode.duration_label(frames, tc["rate"]) if tc.get("rate") and frames else ""
            w.writerow([it["id"], folder, sl.get("scene", ""), sl.get("shot", ""), sl.get("take", ""),
                        "1" if d.get("good") else "", d.get("notes", ""), start, end, dur,
                        it.get("fps") or "", "%dx%d" % (it.get("width") or 0, it.get("height") or 0),
                        it.get("kind", ""), "yes" if it.get("audio") else "no", markers,
                        "" if sync.get("drift_ms") is None else sync["drift_ms"],
                        d.get("drops", ""), datetime.fromtimestamp(t["created"]).strftime("%Y-%m-%d %H:%M:%S"),
                        {True: "yes", False: "no"}.get(d.get("clock_synced"), "")])
    return out.getvalue()
