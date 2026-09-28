"""ARH (Arstro Raw HDMI): uncompressed HDMI capture container.

Designed for straight-to-disk 4K60 capture: frames are stored exactly as the
HDMI RX driver delivers them (NV12 / NV16 / NV24 / BGR ...), each prefixed by
a small chunk header. A file cut short by a crash or a full disk stays
readable up to its last complete chunk.

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
import json
import os
import struct
import time

MAGIC = b"ARH1"
VERSION = 1
HEADER_SIZE = 4096
HEADER = struct.Struct("<4sHHIIQQQQ16x")
CHUNK = struct.Struct("<4sIQQII")
FLAG_FINALIZED = 1

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
                end, index, audio_bytes = _scan_complete(f, size)
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


def _scan_complete(f, size):
    """Walk chunk headers; return (end of last complete chunk, frame index, audio bytes)."""
    pos, index, audio = HEADER_SIZE, [], 0
    while pos + CHUNK.size <= size:
        f.seek(pos)
        ctype, psize, pts, _seq, _, _ = CHUNK.unpack(f.read(CHUNK.size))
        if ctype not in CHUNK_TYPES or pos + CHUNK.size + psize > size:
            break
        if ctype == VFRM:
            index.append((pos, pts))
        elif ctype == AUDS:
            audio += psize
        elif ctype == END:
            break
        pos += CHUNK.size + psize
    return pos, index, audio


class ArhReader:
    """Read an .arh file, optionally following it while it is still being recorded."""

    def __init__(self, path, wait=0.0):
        """wait: seconds to wait for the file/header to appear (for follow mode)."""
        self.path = path
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

    def chunks(self, types=(VFRM, AUDS), follow=False, idle_timeout=15.0, stop=None):
        """Yield (type, pts, seq, payload) for chunks of the given types.

        With follow=True, waits at the end of the file for more data until the
        END chunk arrives, or the file stops growing for idle_timeout seconds
        (recorder crashed). `stop` is an optional threading.Event.
        """
        with open(self.path, "rb") as f:
            pos, last_growth, last_size = HEADER_SIZE, time.monotonic(), -1
            while not (stop and stop.is_set()):
                f.seek(pos)
                head = f.read(CHUNK.size)
                if len(head) == CHUNK.size:
                    ctype, psize, pts, seq, _, _ = CHUNK.unpack(head)
                    if ctype not in CHUNK_TYPES:
                        raise ValueError(f"{self.path}: corrupt chunk at offset {pos}")
                    if ctype == END or ctype == INDX:
                        return
                    if ctype in types:
                        payload = f.read(psize)
                        if len(payload) == psize:
                            yield ctype, pts, seq, payload
                            pos += CHUNK.size + psize
                            continue
                    elif pos + CHUNK.size + psize <= os.fstat(f.fileno()).st_size:
                        pos += CHUNK.size + psize
                        continue
                # incomplete chunk at the end of the file
                if not follow:
                    return
                size = os.fstat(f.fileno()).st_size
                if size != last_size:
                    last_size, last_growth = size, time.monotonic()
                elif time.monotonic() - last_growth > idle_timeout:
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
    """Finalize a recording that was cut off (crash, power loss): keep every complete
    chunk, drop the partial tail, add the index. Returns the frame count, or None if
    the file was already finalized."""
    r = ArhReader(path)
    if r.finalized:
        return None
    w = ArhWriter(path, r.meta)
    w.offset = -1                     # unknown: finalize() scans for complete chunks
    return w.finalize()
