"""Storage you can trust (REC-12): how fast a take fills the disk, and whether the disk keeps up.

needed_rate()  bytes per second the chosen mode writes for the current signal - a RAW take
               writes every frame as it comes (3840x2160 4:2:2 at 29.97 fps: ~500 MB/s).
measure()      writes up to 1 GiB (at most ~3 s) of incompressible data to the storage
               folder, flushed to the device, and times it; the result is kept per
               filesystem (state dir, disk_speed.json) so it is measured once, not per take.
A disk measured slower than a mode needs (with a 15 % margin) refuses that mode's start
unless the user records anyway; the time left is the free space over the rate.
"""

import json
import mmap
import os
import threading
import time

MARGIN = 1.15
AUDIO_PCM = 48000 * 2 * 2                   # S16LE stereo 48 kHz
AUDIO_AAC = 256000 // 8
TEST_BYTES = 1 << 30
TEST_SECONDS = 3.0
BLOCK = 8 << 20

BYTES_PER_PIXEL = {"NV12": 1.5, "NV21": 1.5, "I420": 1.5, "NV16": 2, "NV61": 2, "YUY2": 2, "UYVY": 2,
                   "YVYU": 2, "NV24": 3, "NV42": 3, "BGR": 3, "RGB": 3, "BGRx": 4, "BGRA": 4}

_lock = threading.Lock()


def needed_rate(settings, signal):
    """Bytes/s the main recording writes, or None without a signal."""
    if not signal or not signal.get("present") or not signal.get("width"):
        return None
    audio = settings.get("audio", {}).get("record", True)
    if settings["mode"] == "raw":
        bpp = BYTES_PER_PIXEL.get(signal.get("format") or "", 2)
        return int(signal["width"] * signal["height"] * bpp * (signal.get("fps") or 30)) + (AUDIO_PCM if audio else 0)
    h = settings["h265"]
    peak = 1.5 if h["rc"] == "vbr" else 1.0
    a = (AUDIO_AAC if h["container"] == "mp4" else AUDIO_PCM) if audio else 0
    return int(h["bitrate"] * 1e6 / 8 * peak) + a


def _store():
    from ..paths import state_dir
    return os.path.join(state_dir(), "disk_speed.json")


def _key(folder):
    st = os.stat(folder)
    return "%d:%d" % (os.major(st.st_dev), os.minor(st.st_dev))


def _mount_of(folder):
    """(device, fstype, mount point) of the filesystem holding folder."""
    best = ("?", "?", "/")
    path = os.path.realpath(folder)
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt, fs = line.split()[:3]
                mnt = mnt.replace("\\040", " ")
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best[2]):
                    best = (dev, fs, mnt)
    except OSError:
        pass
    return best


def known(folder):
    """The last measurement of the disk holding folder, or None."""
    try:
        key = _key(folder)
        with _lock, open(_store()) as f:
            return json.load(f).get(key)
    except (OSError, ValueError):
        return None


def measure(folder, max_bytes=TEST_BYTES, seconds=TEST_SECONDS):
    """Time a sequential write to folder; -> the stored result {speed (bytes/s), ...}."""
    path = os.path.join(folder, ".arstro-speed-test")
    buf = mmap.mmap(-1, BLOCK)                   # page aligned (O_DIRECT needs that)
    buf.write(os.urandom(BLOCK))                 # incompressible: some disks compress zeros
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    direct = getattr(os, "O_DIRECT", 0)
    try:
        fd = os.open(path, flags | direct, 0o600)
    except OSError:
        fd, direct = os.open(path, flags, 0o600), 0
    written, t0 = 0, time.monotonic()
    try:
        while written < max_bytes and time.monotonic() - t0 < seconds:
            try:
                n = os.write(fd, buf)
            except OSError:
                if not direct or written:
                    raise
                os.close(fd)                     # this filesystem refuses O_DIRECT writes
                fd, direct = os.open(path, flags, 0o600), 0
                continue
            written += n
        os.fsync(fd)
        elapsed = time.monotonic() - t0
    finally:
        os.close(fd)
        buf.close()
        try:
            os.remove(path)
        except OSError:
            pass
    dev, fs, mnt = _mount_of(folder)
    res = {"speed": int(written / max(elapsed, 1e-3)), "bytes": written, "seconds": round(elapsed, 2),
           "at": int(time.time()), "device": dev, "fs": fs, "mount": mnt, "direct": bool(direct)}
    with _lock:
        try:
            with open(_store()) as f:
                allr = json.load(f)
        except (OSError, ValueError):
            allr = {}
        allr[_key(folder)] = res
        os.makedirs(os.path.dirname(_store()), exist_ok=True)
        tmp = _store() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(allr, f, indent=1)
        os.replace(tmp, _store())
    return res


def verdict(need, speed):
    """(ok, sentence) - ok None when the disk was never measured."""
    if need is None:
        return None, None
    if speed is None:
        return None, ("disk speed not measured yet - test it before a RAW take"
                      if need > 60_000_000 else None)
    if speed >= need * MARGIN:
        return True, None
    return False, ("this disk writes %d MB/s; this mode needs %d MB/s (+15 %% margin) - frames would be "
                   "lost: use H.265, a faster disk (NVMe) or a lower resolution") % (speed // 1_000_000,
                                                                                   need // 1_000_000)


def figures(folder, settings, signal, recording_rate=None, testing=False):
    """The storage block of the recorder status."""
    try:
        st = os.statvfs(folder)
        free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        free = total = 0
    need = needed_rate(settings, signal)
    k = known(folder) if os.path.isdir(folder) else None
    speed = k["speed"] if k else None
    ok, warning = verdict(need, speed)
    rate = recording_rate or need
    return {"path": folder, "free": free, "total": total, "need": need, "rate": rate,
            "seconds_left": int(free / rate) if rate else None, "speed": speed,
            "measured": k, "ok": ok, "warning": warning, "testing": testing}
