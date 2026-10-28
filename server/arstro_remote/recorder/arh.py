"""ARH (Arstro Raw HDMI): uncompressed HDMI capture container.

Designed for straight-to-disk 4K60 capture: frames are stored exactly as the
HDMI RX driver delivers them (NV12 / NV16 / NV24 / BGR ...), each prefixed by
a small chunk header. A file cut short by a crash or a full disk stays
readable up to its last complete chunk.

Damage (GAL-09): a power loss leaves an unfinalized file whose tail is often zero-filled
blocks the filesystem had allocated but never written, sometimes a damaged stretch in the
middle. Readers never stop at a bad chunk header: they resync to the next plausible chunk
(type, payload size and pts all have to fit, and so does the chunk after it) and record the
skipped range in `damaged`. repair() finalizes such a file: it cuts only bytes after the last
valid chunk and records what it did in the metadata ("repair").

All integers are little endian.

File header (4096 bytes)
    0   4s  magic               b"ARH1"
    4   H   version             1
    6   H   flags               bit0 = finalized (recording stopped cleanly)
    8   I   header size         4096
    12  I   JSON metadata length
    16  Q   video frame count   \\
    24  Q   audio byte count     > valid once finalized
    32  Q   duration (ns)        |
    40  Q   index chunk offset  /  (0 = no index, scan the chunks)
    48  16  reserved
    64  ..  JSON metadata (UTF-8), zero padded up to 4096

Chunks follow back to back:
    0   4s  type    b"VFRM" video frame, b"AUDS" PCM audio,
                    b"INDX" frame index, b"END " end of recording
    4   I   payload size
    8   Q   pts (ns) relative to the first video frame
    16  Q   sequence: capture frame counter (VFRM) / first sample number (AUDS)
    24  I   flags (reserved, 0)
    28  I   reserved
    32  ..  payload

INDX payload: one (Q chunk offset, Q pts) pair per VFRM chunk.

JSON metadata:
    {"format": "ARH", "version": 1, "created": ISO-8601, "software": ...,
     "source": {"device": ..., "signal": "3840x2160p60.00"},
     "video": {"format": "NV12", "width", "height", "fps_n", "fps_d",
               "frame_size", "plane_offsets": [...], "plane_strides": [...],
               "colorimetry", "caps"},
     "audio": {"format": "S16LE", "rate": 48000, "channels": 2} | null}
"""
import contextlib
import fcntl
import hashlib
import json
import os
import struct
import tempfile
import time

MAGIC = b"ARH1"
VERSION = 1
HEADER_SIZE = 4096
HEADER = struct.Struct("<4sHHIIQQQQ16x")
CHUNK = struct.Struct("<4sIQQII")
FLAG_FINALIZED = 1
FLAG_REPAIRED = 2                     # finalized by repair() after an interruption
AUDIO_MAX = 8 << 20                   # an AUDS chunk is a few ms of PCM; larger = not a header

VFRM, AUDS, INDX, END = b"VFRM", b"AUDS", b"INDX", b"END "
CHUNK_TYPES = (VFRM, AUDS, INDX, END)


def pack_header(meta, flags=0, frames=0, audio_bytes=0, duration=0, index_offset=0):
    js = json.dumps(meta, separators=(",", ":")).encode()
    if HEADER.size + len(js) > HEADER_SIZE:
        raise ValueError("ARH metadata too large")
    head = HEADER.pack(MAGIC, VERSION, flags, HEADER_SIZE, len(js),
                       frames, audio_bytes, duration, index_offset)
    return (head + js).ljust(HEADER_SIZE, b"\0")


def pack_chunk(ctype, size, pts=0, seq=0):
    return CHUNK.pack(ctype, size, pts, seq, 0, 0)


class ArhWriter:
    """Book-keeping for a file whose bytes are written by someone else (filesink).

    header() / video_chunk() / audio_chunk() return the bytes to write and track
    offsets; finalize() runs after the writer closed the file and appends the
    index + end marker and patches the header.
    """

    def __init__(self, path, meta):
        self.path = path
        self.meta = meta
        self.offset = HEADER_SIZE
        self.index = []
        self.audio_bytes = 0
        self.last_pts = 0

    def header(self):
        return pack_header(self.meta)

    def video_chunk(self, size, pts, seq):
        self.index.append((self.offset, pts))
        self.offset += CHUNK.size + size
        self.last_pts = max(self.last_pts, pts)
        return pack_chunk(VFRM, size, pts, seq)

    def audio_chunk(self, size, pts, seq):
        self.offset += CHUNK.size + size
        self.audio_bytes += size
        return pack_chunk(AUDS, size, pts, seq)

    @property
    def frames(self):
        return len(self.index)

    def finalize(self):
        """Close the file properly. Returns the number of video frames kept."""
        size = os.path.getsize(self.path)
        index, audio_bytes = self.index, self.audio_bytes
        if size != self.offset:
            # The writer stopped early (disk full, error): keep complete chunks only.
            with open(self.path, "rb") as f:
                end, index, audio_bytes = _scan_complete(f, size, self.meta["video"]["frame_size"])
            with open(self.path, "r+b") as f:
                f.truncate(end)
            size = end
        fps_n, fps_d = self.meta["video"]["fps_n"], self.meta["video"]["fps_d"]
        duration = (index[-1][1] + 10**9 * fps_d // fps_n) if index else 0
        with open(self.path, "r+b") as f:
            f.seek(size)
            payload = b"".join(struct.pack("<QQ", o, p) for o, p in index)
            f.write(pack_chunk(INDX, len(payload)) + payload)
            f.write(pack_chunk(END, 0, duration))
            f.seek(0)
            f.write(pack_header(self.meta, FLAG_FINALIZED, len(index), audio_bytes,
                                duration, size))
        return len(index)


def _plausible(ctype, psize, frame_size):
    if ctype == VFRM:
        return psize == frame_size
    if ctype == AUDS:
        return 0 < psize <= AUDIO_MAX
    if ctype == INDX:
        return psize % 16 == 0
    return ctype == END and psize == 0


def _chunk_ok(f, pos, size, frame_size, min_pts, depth=2):
    """A plausible chunk header at pos whose payload fits, followed by another one (or the
    end of the file) - two in a row make a false match inside pixel data very unlikely."""
    if pos + CHUNK.size > size:
        return False
    f.seek(pos)
    ctype, psize, pts, _seq, _, _ = CHUNK.unpack(f.read(CHUNK.size))
    if not _plausible(ctype, psize, frame_size) or pos + CHUNK.size + psize > size:
        return False
    if ctype in (INDX, END):
        return True
    if pts + 10**9 < min_pts:                          # time never runs backwards
        return False
    nxt = pos + CHUNK.size + psize
    return depth <= 1 or nxt + CHUNK.size > size or _chunk_ok(f, nxt, size, frame_size, pts, depth - 1)


def resync(f, pos, size, frame_size, min_pts=0, block=64 << 20):
    """Offset of the next valid chunk after a bad one at pos, or None (nothing valid follows)."""
    p = pos + 1
    while p + CHUNK.size <= size:
        f.seek(p)
        buf = f.read(block + CHUNK.size)
        i = 0
        while True:
            hits = [j for j in (buf.find(t, i) for t in CHUNK_TYPES) if j >= 0]
            if not hits:
                break
            j = min(hits)
            if _chunk_ok(f, p + j, size, frame_size, min_pts):
                return p + j
            i = j + 1
        p += block
    return None


def scan(f, size, frame_size):
    """Walk all chunks, resyncing over damage. Returns {"end": end of the last valid chunk,
    "index": [(offset, pts)] video, "audio": bytes, "damaged": [(start, end)], "tail": offset
    of INDX/END or None}."""
    pos, index, audio, damaged, last_pts, end, tail = HEADER_SIZE, [], 0, [], 0, HEADER_SIZE, None
    while pos + CHUNK.size <= size:
        f.seek(pos)
        ctype, psize, pts, _seq, _, _ = CHUNK.unpack(f.read(CHUNK.size))
        if _plausible(ctype, psize, frame_size) and pos + CHUNK.size + psize <= size:
            if ctype in (INDX, END):
                tail = pos
                break
            if ctype == VFRM:
                index.append((pos, pts))
                last_pts = max(last_pts, pts)
            else:
                audio += psize
            pos += CHUNK.size + psize
            end = pos
            continue
        nxt = resync(f, pos, size, frame_size, last_pts)
        damaged.append((pos, nxt if nxt is not None else size))
        if nxt is None:
            pos = size
            break
        pos = nxt
    if tail is None and end < size and not (damaged and damaged[-1][1] == size):
        damaged.append((end, size))                 # a partial last chunk
    return {"end": end, "index": index, "audio": audio, "damaged": damaged, "tail": tail}


def _scan_complete(f, size, frame_size):
    """(end of the last valid chunk, frame index, audio bytes)."""
    r = scan(f, size, frame_size)
    return r["end"], r["index"], r["audio"]


class ArhReader:
    """Read an .arh file, optionally following it while it is still being recorded."""

    def __init__(self, path, wait=0.0):
        """wait: seconds to wait for the file/header to appear (for follow mode)."""
        self.path = path
        self.damaged = set()          # (start, end) byte ranges chunks() skipped
        deadline = time.monotonic() + wait
        while True:
            try:
                if os.path.getsize(path) >= HEADER_SIZE:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise FileNotFoundError(f"{path}: no ARH header")
            time.sleep(0.2)
        self.reload_header()

    def reload_header(self):
        with open(self.path, "rb") as f:
            raw = f.read(HEADER_SIZE)
        (magic, version, self.flags, hsize, jlen, self.frame_count, self.audio_bytes,
         self.duration, self.index_offset) = HEADER.unpack_from(raw)
        if magic != MAGIC:
            raise ValueError(f"{self.path}: not an ARH file")
        if version > VERSION:
            raise ValueError(f"{self.path}: ARH version {version} not supported")
        self.meta = json.loads(raw[HEADER.size:HEADER.size + jlen])
        self.video = self.meta["video"]
        self.audio = self.meta.get("audio")

    @property
    def finalized(self):
        return bool(self.flags & FLAG_FINALIZED)

    @property
    def repaired(self):
        """What repair() did, or None: {"frames", "dropped_bytes", "damaged": [[start, end]], "at"}."""
        return self.meta.get("repair") if self.flags & FLAG_REPAIRED else None

    @property
    def fps(self):
        return self.video["fps_n"] / self.video["fps_d"]

    def estimate_frames(self):
        """Frame count: exact once finalized, else estimated from the file size."""
        if self.finalized:
            return self.frame_count
        per = CHUNK.size + self.video["frame_size"]
        if self.audio:
            a = self.audio
            per += a["rate"] * a["channels"] * 2 / self.fps
        return int((os.path.getsize(self.path) - HEADER_SIZE) / per)

    def chunks(self, types=(VFRM, AUDS), follow=False, idle_timeout=15.0, stop=None, read=True):
        """Yield (type, pts, seq, payload) for chunks of the given types.

        With follow=True, waits at the end of the file for more data until the
        END chunk arrives, or the file stops growing for idle_timeout seconds
        (recorder crashed). `stop` is an optional threading.Event. With read=False
        the payload is not read: (offset, size) of it in the file instead.
        """
        frame_size = self.video["frame_size"]
        with open(self.path, "rb") as f:
            pos, last_growth, last_size, last_pts = HEADER_SIZE, time.monotonic(), -1, 0
            while not (stop and stop.is_set()):
                size = os.fstat(f.fileno()).st_size
                if size != last_size:
                    last_size, last_growth = size, time.monotonic()
                stalled = not follow or time.monotonic() - last_growth > idle_timeout
                f.seek(pos)
                head = f.read(CHUNK.size)
                if len(head) == CHUNK.size:
                    ctype, psize, pts, seq, _, _ = CHUNK.unpack(head)
                    if _plausible(ctype, psize, frame_size):
                        if ctype == END or ctype == INDX:
                            return
                        if pos + CHUNK.size + psize <= size:
                            if ctype == VFRM:
                                last_pts = max(last_pts, pts)
                            if ctype in types:
                                payload = f.read(psize) if read else (pos + CHUNK.size, psize)
                                if not read or len(payload) == psize:
                                    yield ctype, pts, seq, payload
                                    pos += CHUNK.size + psize
                                    continue
                            else:
                                pos += CHUNK.size + psize
                                continue
                    elif stalled:
                        # not a chunk (power-loss damage): skip to the next valid one
                        nxt = resync(f, pos, size, frame_size, last_pts)
                        self.damaged.add((pos, nxt if nxt is not None else size))
                        if nxt is None:
                            return
                        pos = nxt
                        continue
                # incomplete chunk at the end of the file
                if stalled:
                    if pos < size:
                        self.damaged.add((pos, size))
                    return
                time.sleep(0.05)


def chunk_index(path):
    """Positions of all chunks, for random access (playback, seeking):
    ([(offset, pts, seq)] video frames, [(offset, pts, size)] audio chunks).
    Offsets point at the payload. Only complete chunks are listed."""
    video, audio = [], []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = HEADER_SIZE
        while pos + CHUNK.size <= size:
            f.seek(pos)
            ctype, psize, pts, seq, _, _ = CHUNK.unpack(f.read(CHUNK.size))
            if ctype not in CHUNK_TYPES or pos + CHUNK.size + psize > size:
                break
            if ctype == VFRM:
                video.append((pos + CHUNK.size, pts, seq))
            elif ctype == AUDS:
                audio.append((pos + CHUNK.size, pts, psize))
            elif ctype in (INDX, END):
                break
            pos += CHUNK.size + psize
    return video, audio


def info(path):
    r = ArhReader(path)
    v = r.video
    lines = [
        f"file:      {path}",
        f"state:     {'finalized' if r.finalized else 'NOT finalized (recording or interrupted)'}",
        f"video:     {v['width']}x{v['height']} {v['format']} @ {r.fps:.3f} fps, "
        f"{v['frame_size']} bytes/frame",
        f"audio:     {r.audio['format']} {r.audio['rate']} Hz {r.audio['channels']} ch"
        if r.audio else "audio:     none",
        f"frames:    {r.estimate_frames()}{'' if r.finalized else ' (estimated)'}",
    ]
    if r.finalized:
        lines.append(f"duration:  {r.duration / 1e9:.2f} s")
    src = r.meta.get("source", {})
    lines.append(f"source:    {src.get('signal', '?')} from {src.get('device', '?')}, "
                 f"recorded {r.meta.get('created', '?')}")
    return "\n".join(lines)


def repair(path):
    """Finalize a recording that was cut off (crash, power loss, GAL-09): keep every valid
    chunk (resyncing over damage), cut only what follows the last one, add the index and
    note the repair in the metadata. Returns ArhReader.repaired, or None if the file was
    already finalized. The caller makes sure nobody writes the file any more (in_use)."""
    r = ArhReader(path)
    if r.finalized:
        return None
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        sc = scan(f, size, r.video["frame_size"])
    end, index = sc["end"], sc["index"]
    fps_n, fps_d = r.video["fps_n"], r.video["fps_d"]
    duration = (index[-1][1] + 10**9 * fps_d // fps_n) if index else 0
    meta = dict(r.meta, repair={"frames": len(index), "dropped_bytes": size - end,
                                "damaged": [list(d) for d in sc["damaged"]],
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    try:
        head = pack_header(meta, FLAG_FINALIZED | FLAG_REPAIRED, len(index), sc["audio"], duration, end)
    except ValueError:                                 # no room for the note: keep it short
        meta["repair"]["damaged"] = meta["repair"]["damaged"][:3]
        head = pack_header(meta, FLAG_FINALIZED | FLAG_REPAIRED, len(index), sc["audio"], duration, end)
    with open(path, "r+b") as f:
        f.truncate(end)
        f.seek(end)
        payload = b"".join(struct.pack("<QQ", o, p) for o, p in index)
        f.write(pack_chunk(INDX, len(payload)) + payload)
        f.write(pack_chunk(END, 0, duration))
        f.flush()
        os.fsync(f.fileno())
        f.seek(0)
        f.write(head)
        f.flush()
        os.fsync(f.fileno())
    return meta["repair"]


def in_use(path):
    """Is any process (of ours) holding the file open for writing? (/proc/*/fdinfo flags)"""
    target = os.path.realpath(path)
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") != target:
                    continue
                with open(f"/proc/{pid}/fdinfo/{fd}") as fi:
                    flags = int(next(l for l in fi if l.startswith("flags:")).split()[1], 8)
                if flags & (os.O_WRONLY | os.O_RDWR):
                    return True
            except (OSError, StopIteration, ValueError):
                continue
    return False


def needs_repair(path, quiet_s=60):
    """An unfinalized file nobody writes and that has not grown for quiet_s: interrupted."""
    try:
        r = ArhReader(path)
        if r.finalized:
            return False
        if time.time() - os.path.getmtime(path) < quiet_s:
            return False
    except (OSError, ValueError):
        return False
    return not in_use(path)


def cfr_frames(reader, follow=False, stop=None, max_fill_s=10, read=True):
    """The video as a constant frame rate sequence: (pts, seq, payload, repeated) - a gap in
    the capture sequence (dropped frames, a damaged stretch) repeats the previous frame, at
    most max_fill_s worth. The encoders and the lossless check walk exactly this sequence.
    With read=False the payload is its (offset, size) in the file (chunks)."""
    cap = int(reader.fps * max_fill_s)
    last_seq, prev = None, None
    for _t, pts, seq, data in reader.chunks((VFRM,), follow, stop=stop, read=read):
        if last_seq is not None and prev is not None:
            for _ in range(min(max(0, seq - last_seq - 1), cap)):
                yield pts, seq, prev, True
        last_seq, prev = seq, data
        yield pts, seq, data, False


@contextlib.contextmanager
def file_lock(path):
    """Exclusive, non-blocking lock named after a file, shared by every slot of this user
    (A and B see the same recordings): yields True if we hold it, False if someone else does."""
    name = "arstro-%s.lock" % hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:16]
    lock = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), name)
    with open(lock, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def repair_if_needed(path, log=None):
    """repair() an interrupted recording once it is safe (nobody writes it, nobody else is
    repairing it). Returns the repair note, or None when nothing was done."""
    if not needs_repair(path):
        return None
    with file_lock(path) as mine:
        if not mine or not needs_repair(path):
            return None
        note = repair(path)
        if log and note:
            log("repaired %s: %d frames kept, %d damaged bytes cut" % (
                os.path.basename(path), note["frames"], note["dropped_bytes"]))
        return note
