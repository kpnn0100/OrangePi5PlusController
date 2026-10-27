"""Time-of-day timecode (REC-11): what an editor lines takes up by.

Every take starts at the time of day of its first frame, read from the board's clock
(kept right by NTP), as SMPTE timecode at the take's frame rate:

    25 fps        10:11:12:13      (13 = frame within the second)
    29.97 / 59.94 10:11:12;13      drop-frame: labels ;00 and ;01 (;00-;03 at 59.94) are
                                   skipped every minute except every tenth, so the label
                                   stays on the wall clock
    23.976        10:11:12:13      counted at 24 (non-drop, the convention)

The start timecode goes into each file where editors read it: a QuickTime `tmcd` track
in .mov / .mp4 (add_tmcd, after the file is closed - the muxers here cannot write one),
ffmpeg's `-timecode` for the files ffmpeg writes (FFV1 .mkv), the ARH header's metadata
and the take's sidecar (takes.py) for the rest.
"""

import os
import struct
import subprocess
import time
from datetime import datetime


def rate_of(fps_n, fps_d):
    """-> (frames per second as counted, drop frame?, frames dropped per minute)."""
    fps_n, fps_d = int(fps_n), int(fps_d) or 1
    base = max(1, round(fps_n / fps_d))
    drop = fps_d == 1001 and base in (30, 60)
    return base, drop, (2 if base == 30 else 4) if drop else 0


def frames_from_label(h, m, s, f, base, drop, dropn):
    count = ((h * 3600 + m * 60 + s) * base) + f
    if drop:
        minutes = h * 60 + m
        count -= dropn * (minutes - minutes // 10)
    return count


def label_from_frames(count, base, drop, dropn):
    """Frame count since midnight -> 'HH:MM:SS:FF' (';' before the frames when drop frame)."""
    day = base * 86400 - (dropn * (1440 - 144) if drop else 0)
    count %= day
    if drop:
        ten = base * 600 - dropn * 9               # frames per ten minutes
        per_min = base * 60 - dropn                # frames per (dropped) minute
        d, mm = divmod(count, ten)
        count += dropn * 9 * d + (dropn * ((mm - dropn) // per_min) if mm > dropn else 0)
    f = count % base
    s = count // base % 60
    m = count // (base * 60) % 60
    h = count // (base * 3600) % 24
    return "%02d:%02d:%02d%s%02d" % (h, m, s, ";" if drop else ":", f)


def start_timecode(wall, fps_n, fps_d):
    """Timecode of a frame captured at `wall` (seconds since the epoch, local time of day)."""
    base, drop, dropn = rate_of(fps_n, fps_d)
    t = datetime.fromtimestamp(wall)
    f = min(base - 1, int((wall - int(wall)) * base))
    if drop and t.minute % 10 and t.second == 0 and f < dropn:
        f = dropn                                  # that label does not exist: the next one does
    frames = frames_from_label(t.hour, t.minute, t.second, f, base, drop, dropn)
    return {"tc": label_from_frames(frames, base, drop, dropn), "frames": frames,
            "rate": "%d/%d" % (int(fps_n), int(fps_d) or 1), "base": base, "drop": drop,
            "wallclock": datetime.fromtimestamp(wall).isoformat(timespec="milliseconds")}


def offset_label(tc, frames):
    """The label `frames` frames after a start timecode dict (a marker, the end of a take)."""
    base, drop, dropn = rate_of(*tc["rate"].split("/"))
    return label_from_frames(tc["frames"] + int(frames), base, drop, dropn)


def duration_label(frames, rate):
    """A duration in frames as a timecode from 00:00:00:00 (CSV 'Duration TC')."""
    base, drop, dropn = rate_of(*rate.split("/"))
    return label_from_frames(int(frames), base, drop, dropn)


_synced = (0.0, None)


def clock_synced():
    """True / False when the system says whether NTP has set the clock, None if it can't tell.
    Asked at most every 30 s (it runs timedatectl)."""
    global _synced
    now = time.monotonic()
    if now - _synced[0] < 30 and _synced[0]:
        return _synced[1]
    val = None
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        val = {"yes": True, "no": False}.get(out)
    except (OSError, subprocess.SubprocessError):
        pass
    _synced = (now, val)
    return val


# ------------------------------------------------------------------ the tmcd track
# Laid out like ffmpeg's (`-timecode`), which Resolve, Premiere and Final Cut read.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"tref", b"dinf", b"udta"}
_GMHD = bytes.fromhex(
    "00000018676d696e000000000040800080008000000000000000002c7465787400010000000000000000"
    "000000000000000100000000000000000000000000004000000000000036746d63640000002e74636d69"
    "0000000000000000000c0000000000000000ffffffffffff0d4c7563696461204772616e6465")


def _box(typ, *parts):
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), typ) + body


def _children(data, off, end):
    """[(type, offset, header size, size)] of the boxes in data[off:end]."""
    out = []
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", data, off)
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack_from(">Q", data, off + 8)[0], 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            raise ValueError("damaged box %r at %d" % (typ, off))
        out.append((typ, off, hdr, size))
        off += size
    return out


def _find(data, off, end, typ):
    for t, o, h, s in _children(data, off, end):
        if t == typ:
            return o, h, s
    return None


def _tmcd_trak(track_id, movie_dur, movie_scale, fps_n, fps_d, base, drop, sample_offset):
    media_dur = movie_dur * fps_n // movie_scale if movie_scale else 0
    # version 0, flags 2 (in the movie, not shown), 32-bit times, no volume, unity matrix, 0x0
    tkhd = struct.pack(">IIIII I 8x hhH 2x 36s II", 0x2, 0, 0, track_id, 0, movie_dur, 0, 0, 0,
                       bytes.fromhex("000100000000000000000000000000000001000000000000"
                                     "000000000000000040000000"), 0, 0)
    elst = _box(b"elst", struct.pack(">I I IIhh", 0, 1, movie_dur, 0, 1, 0))
    mdhd = _box(b"mdhd", struct.pack(">I II I I HH", 0, 0, 0, fps_n, media_dur, 0x7FFF, 0))
    hdlr = _box(b"hdlr", struct.pack(">I4s4s12s", 0, b"mhlr", b"tmcd", b"\0" * 12),
                bytes([15]) + b"TimeCodeHandler")
    dhlr = _box(b"hdlr", struct.pack(">I4s4s12s", 0, b"dhlr", b"url ", b"\0" * 12),
                bytes([11]) + b"DataHandler")
    dinf = _box(b"dinf", _box(b"dref", struct.pack(">II", 0, 1), _box(b"url ", struct.pack(">I", 1))))
    entry = _box(b"tmcd", b"\0" * 6, struct.pack(">H I I I I B B H", 1, 0, 1 if drop else 0,
                                                 fps_n, fps_d, base, 0, 0))
    stbl = _box(b"stbl",
                _box(b"stsd", struct.pack(">II", 0, 1), entry),
                _box(b"stts", struct.pack(">II II", 0, 1, 1, media_dur)),
                _box(b"stsc", struct.pack(">II III", 0, 1, 1, 1, 1)),
                _box(b"stsz", struct.pack(">I II", 0, 4, 1)),
                _box(b"stco", struct.pack(">II I", 0, 1, sample_offset)))
    minf = _box(b"minf", _box(b"gmhd", _GMHD), dhlr, dinf, stbl)
    return _box(b"trak", _box(b"tkhd", tkhd), _box(b"edts", elst), _box(b"mdia", mdhd, hdlr, minf))


def _video_trak(data, moov_off, moov_hdr, moov_size):
    for t, o, h, s in _children(data, moov_off + moov_hdr, moov_off + moov_size):
        if t != b"trak":
            continue
        mdia = _find(data, o + h, o + s, b"mdia")
        if not mdia:
            continue
        hd = _find(data, mdia[0] + mdia[1], mdia[0] + mdia[2], b"hdlr")
        if hd and data[hd[0] + hd[1] + 8:hd[0] + hd[1] + 12] == b"vide":
            return o, h, s
    return None


def read_tmcd(path):
    """The start frame count of a file's tmcd track, or None (tests, the gallery).
    Reads only the moov and the one sample, never the media."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        moov = next(((o, h, s) for t, o, h, s in _children_from_file(f, size) if t == b"moov"), None)
        if not moov:
            return None
        f.seek(moov[0])
        data = f.read(moov[2])
        for t, o, h, s in _children(data, moov[1], len(data)):
            if t != b"trak":
                continue
            mdia = _find(data, o + h, o + s, b"mdia")
            hd = mdia and _find(data, mdia[0] + mdia[1], mdia[0] + mdia[2], b"hdlr")
            if hd and data[hd[0] + hd[1] + 8:hd[0] + hd[1] + 12] == b"tmcd":
                i = data.find(b"stco", o, o + s)
                f.seek(struct.unpack_from(">I", data, i + 12)[0])
                return struct.unpack(">I", f.read(4))[0]
    return None


def add_tmcd(path, tc):
    """Give a closed .mov / .mp4 a timecode track starting at `tc` (start_timecode's dict).

    The media data is not moved: the track's one sample (the start frame number) goes in a
    small mdat at the end, the new moov replaces the old one where it fits (qtmux reserves
    room at the head) or goes at the end with the old one turned into free space.
    Returns True when the track was added (False: the file already has one)."""
    fps_n, fps_d = (int(x) for x in tc["rate"].split("/"))
    base, drop, _ = rate_of(fps_n, fps_d)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        top = _children_from_file(f, size)
        moov = next(((o, h, s) for t, o, h, s in top if t == b"moov"), None)
        if not moov:
            raise ValueError("no moov box (file not closed?)")
        f.seek(moov[0])
        data = f.read(moov[2])
    moov_off = moov[0]
    local = (0, moov[1], moov[2])
    if b"tmcd" in data and read_tmcd_in(data):
        return False
    mvhd = _find(data, local[1], local[2], b"mvhd")
    if not mvhd:
        raise ValueError("no mvhd box")
    mo = mvhd[0] + mvhd[1]
    version = data[mo]
    if version == 1:
        scale, dur = struct.unpack_from(">IQ", data, mo + 20)
        next_id_at = mo + 4 + 8 + 8 + 4 + 8 + 4 + 2 + 10 + 36 + 24
    else:
        scale, dur = struct.unpack_from(">II", data, mo + 12)
        next_id_at = mo + 4 + 4 + 4 + 4 + 4 + 4 + 2 + 10 + 36 + 24
    track_id = struct.unpack_from(">I", data, next_id_at)[0]
    vt = _video_trak(data, *local)
    if not vt:
        raise ValueError("no video track")

    sample_offset = size + 8                         # inside the mdat appended at the end
    trak = _tmcd_trak(track_id, dur, scale, fps_n, fps_d, base, drop, sample_offset)
    # the video track points at the timecode track (tref/tmcd)
    vo, vh, vs = vt
    tref = _find(data, vo + vh, vo + vs, b"tref")
    if tref:
        ro, rh, rs = tref
        new_tref = data[ro:ro + rs] + _box(b"tmcd", struct.pack(">I", track_id))
        new_tref = struct.pack(">I", len(new_tref)) + new_tref[4:]
        video = data[vo:ro] + new_tref + data[ro + rs:vo + vs]
    else:
        video = data[vo:vo + vs] + _box(b"tref", _box(b"tmcd", struct.pack(">I", track_id)))
    video = struct.pack(">I", len(video)) + video[4:]
    body = bytearray(data[local[1]:vo] + video + data[vo + vs:local[2]] + trak)
    struct.pack_into(">I", body, next_id_at - local[1], track_id + 1)
    new_moov = struct.pack(">I4s", 8 + len(body), b"moov") + bytes(body)

    with open(path, "r+b") as f:
        f.seek(size)
        f.write(_box(b"mdat", struct.pack(">I", tc["frames"])))
        # room after the moov: the free box qtmux leaves when it reserves the moov's place
        room = moov[2]
        nxt = next(((t, o, h, s) for t, o, h, s in top if o == moov_off + moov[2]), None)
        if nxt and nxt[0] == b"free":
            room += nxt[3]
        if len(new_moov) == room or len(new_moov) + 8 <= room:
            f.seek(moov_off)
            f.write(new_moov)
            if room > len(new_moov):
                f.write(struct.pack(">I4s", room - len(new_moov), b"free"))
        else:
            f.seek(size + 12)
            f.write(new_moov)
            f.seek(moov_off + 4)
            f.write(b"free")                         # the old moov becomes padding
        f.flush()
        os.fsync(f.fileno())
    return True


def read_tmcd_in(moov):
    for t, o, h, s in _children(moov, 8, len(moov)):
        if t == b"trak":
            mdia = _find(moov, o + h, o + s, b"mdia")
            hd = mdia and _find(moov, mdia[0] + mdia[1], mdia[0] + mdia[2], b"hdlr")
            if hd and moov[hd[0] + hd[1] + 8:hd[0] + hd[1] + 12] == b"tmcd":
                return True
    return False


def _children_from_file(f, size):
    out, off = [], 0
    while off + 8 <= size:
        f.seek(off)
        head = f.read(16)
        sz, typ = struct.unpack_from(">I4s", head)
        hdr = 8
        if sz == 1:
            sz, hdr = struct.unpack_from(">Q", head, 8)[0], 16
        elif sz == 0:
            sz = size - off
        if sz < hdr:
            break
        out.append((typ, off, hdr, sz))
        off += sz
    return out
