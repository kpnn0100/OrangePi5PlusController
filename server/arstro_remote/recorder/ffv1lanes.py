"""FFV1 from a RAW take on the CPU and the GPU at once: the fastest lossless copy (GAL-04).

The take's frames are handed out in order to whichever *lane* has room: FFmpeg processes
encoding FFV1 on the CPU (two, each on every core it may use: one alone leaves cores idle while
it waits for the slowest slice of a frame) and one on the GPU (Vulkan, ffv1_vulkan). All write
the same FFV1 configuration - coder, context, 256 slices; the header bytes are compared and a
lane that differs is dropped - so their frames belong to one stream: the packets are put back
in frame order and streamed into another FFmpeg that only muxes (video copied, the RAW's audio
as PCM, the take's timecode) into the .mkv. Nothing is encoded or written twice.

Measured on a 4K 4:2:2 camera take read from the SSD (FFmpeg 9, 4.2 MB per FFV1 frame): two
CPU lanes + GPU 16.5 fps, one CPU lane + GPU 15.3, one CPU lane 11.7; the single GStreamer
encoder (system FFmpeg 5.1) made 9.5. 256 slices are 1.6% larger than 24 and keep the GPU
fast (at 64 slices it drops from 4.9 to 1.9 fps).

A lane that dies or hangs (a GPU driver error) gives its unfinished frames back and the other
lanes - or a fresh CPU lane - encode them, so the job still finishes. The lossless check
(GAL-08) proves the joined file frame by frame.
"""

import collections
import fcntl
import mmap
import os
import queue
import struct
import subprocess
import sys
import threading
import time

SLICES = 256
DEPTH = {"cpu": 4, "gpu": 4}           # frames a lane holds: enough to keep its pipeline busy
SPEED = {"cpu": 2, "gpu": 1}           # which lane first when several have room
MAX_SPAN = 48                          # frames done ahead of the oldest unfinished one (memory)
RESCUES = 2                            # fresh CPU lanes when no lane is left
STALL_S = 60                           # a lane holding frames, none back for this long: stuck
LANE_NICE = 3                          # encoders below the process feeding them: reading never starves
PIPE_SIZE = 1 << 20                    # a frame goes through a lane's input pipe in 16 steps, not 260

# Matroska element IDs (marker bits kept, as written)
EBML, SEGMENT, CLUSTER, BLOCKGROUP = 0x1A45DFA3, 0x18538067, 0x1F43B675, 0xA0
INFO, TRACKS, TRACKENTRY, CODECPRIVATE = 0x1549A966, 0x1654AE6B, 0xAE, 0x63A2
TIMESTAMPSCALE, TIMECODE, SIMPLEBLOCK, BLOCK = 0x2AD7B1, 0xE7, 0xA3, 0xA1
CONTAINERS = (SEGMENT, CLUSTER, BLOCKGROUP)   # read through, whatever their size says
UNKNOWN_SIZE = b"\x01\xff\xff\xff\xff\xff\xff\xff"


# ---------------------------------------------------------------- Matroska, just enough

def _vint(read, marker):
    """One EBML variable-length integer from read(n): (value, its bytes); None at the end."""
    b = read(1)
    if not b:
        return None
    first, n = b[0], 1
    while n <= 8 and not first & (0x80 >> (n - 1)):
        n += 1
    if n > 8:
        raise ValueError("not a Matroska stream")
    raw = b + (read(n - 1) if n > 1 else b"")
    if len(raw) < n:
        raise EOFError
    value = int.from_bytes(raw, "big")
    if not marker:
        value &= (1 << (7 * n)) - 1
        if value == (1 << (7 * n)) - 1:
            value = -1                  # unknown size
    return value, raw


def _children(buf):
    """(id, data) of the elements in a buffer."""
    pos = 0

    def read(n):
        nonlocal pos
        pos += n
        return buf[pos - n:pos]
    while pos < len(buf):
        eid, _ = _vint(read, True)
        size, _ = _vint(read, False)
        yield eid, read(size if size >= 0 else len(buf) - pos)


def _uint(buf):
    return int.from_bytes(buf, "big") if buf else 0


def element(eid, data):
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big") + b"\x01" + len(data).to_bytes(7, "big") + data


class MkvStream:
    """Reads the Matroska stream an FFmpeg writes to a pipe: its header elements, then the
    frames (block payloads) in order."""

    def __init__(self, f):
        self.f = f
        self.ebml = self.info = self.tracks = None
        self.codec_private = b""
        self.scale = 1000000            # ns per timestamp unit

    def _read(self, n):
        out = self.f.read(n)
        if len(out) < n:
            raise EOFError
        return out

    def _next(self):
        """(id, size, header bytes) of the next element; None at a clean end."""
        h = _vint(lambda n: self.f.read(n), True)
        if h is None:
            return None
        eid, raw_id = h
        size, raw_size = _vint(self._read, False)
        return eid, size, raw_id + raw_size

    def header(self):
        """Reads up to the first frame; ebml / info / tracks are kept as whole elements."""
        while True:
            el = self._next()
            if el is None:
                raise EOFError
            eid, size, head = el
            if eid in CONTAINERS:
                if eid == CLUSTER:
                    return self
                continue
            data = self._read(size)
            if eid == EBML:
                self.ebml = head + data
            elif eid == INFO:
                self.info = head + data
                for cid, val in _children(data):
                    if cid == TIMESTAMPSCALE:
                        self.scale = _uint(val) or self.scale
            elif eid == TRACKS:
                self.tracks = head + data
                for cid, entry in _children(data):
                    if cid == TRACKENTRY:
                        self.codec_private = next((v for i, v in _children(entry) if i == CODECPRIVATE), b"")
                        break

    def frames(self):
        """The frame payloads in order (one video track, no lacing)."""
        while True:
            el = self._next()
            if el is None:
                return
            eid, size, _head = el
            if eid in CONTAINERS:
                continue
            data = self._read(size)
            if eid in (SIMPLEBLOCK, BLOCK):
                n = 1                       # track number (a vint), timestamp (2), flags (1)
                while n < 8 and not data[0] & (0x80 >> (n - 1)):
                    n += 1
                if data[n + 2] & 0x06:
                    raise ValueError("laced Matroska blocks")
                yield data[n + 3:]


def stream_header(ref):
    """The joined stream's start: the reference lane's EBML, Info and Tracks in an open Segment."""
    return ref.ebml + element(SEGMENT, b"")[:4] + UNKNOWN_SIZE + ref.info + ref.tracks


def cluster(ts, payload):
    """One frame as a cluster of its own (a keyframe: every FFV1 frame is)."""
    return element(CLUSTER, element(TIMECODE, struct.pack(">Q", ts)) +
                   element(SIMPLEBLOCK, b"\x81\x00\x00\x80" + payload))


# ---------------------------------------------------------------- lanes

class FrameReader:
    """Reads frames of the RAW by (offset, size) into one reused page-aligned buffer, with
    O_DIRECT where the filesystem allows it: a frame is one large request the drive serves many
    pieces of at once, straight into this buffer - no page cache copy, no fresh memory per frame.
    (Buffered reads of new 16.6 MB objects took 50-70 ms a frame while every core encoded; this
    one thread then held back the whole job. posix_fadvise WILLNEED reads only 256 KB ahead.)"""

    ALIGN = 4096

    def __init__(self, path):
        try:
            self.fd, self.direct = os.open(path, os.O_RDONLY | os.O_DIRECT), True
        except (OSError, AttributeError):        # tmpfs and others: plain reads
            self.fd, self.direct = os.open(path, os.O_RDONLY), False
        self.buf = None

    def read(self, off, size):
        """The frame as a memoryview of the buffer (valid until the next read)."""
        start = off - off % self.ALIGN if self.direct else off
        need = off + size - start
        length = -(-need // self.ALIGN) * self.ALIGN if self.direct else need
        if self.buf is None or len(self.buf) < length:
            self.buf = mmap.mmap(-1, length)
        view, got = memoryview(self.buf), 0
        while got < need:
            n = os.preadv(self.fd, [view[got:length]], start + got)
            if not n:
                raise OSError("the RAW ended early")
            got += n
        return view[off - start:off - start + size]

    def close(self):
        os.close(self.fd)


def lane_cmd(kind, exe, v, planar, pixfmt, threads):
    """FFmpeg reading raw frames on stdin, writing FFV1 in Matroska on stdout."""
    cmd = [exe, "-hide_banner", "-nostdin", "-v", "error"]
    if kind == "gpu":
        cmd += ["-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk"]
    cmd += ["-f", "rawvideo", "-pix_fmt", pixfmt, "-s", f"{v['width']}x{v['height']}",
            "-framerate", f"{v['fps_n']}/{v['fps_d']}", "-i", "pipe:0"]
    if kind == "gpu":
        cmd += ["-vf", f"format={planar},hwupload", "-c:v", "ffv1_vulkan"]
    else:
        cmd += ["-vf", f"format={planar}", "-c:v", "ffv1", "-level", "3", "-g", "1",
                "-threads", str(threads)]
    return cmd + ["-coder", "rice", "-context", "0", "-slices", str(SLICES), "-slicecrc", "1",
                  "-f", "matroska", "pipe:1"]


class Lane:
    def __init__(self, kind, cmd, env):
        self.kind, self.depth = kind, DEPTH[kind]
        self.proc = subprocess.Popen(["nice", "-n", str(LANE_NICE)] + cmd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                fcntl.fcntl(pipe, getattr(fcntl, "F_SETPIPE_SZ", 1031), PIPE_SIZE)
            except OSError:
                pass                    # a smaller pipe-max-size: the default size works too
        self.inbox = queue.Queue()
        self.fed = collections.deque()   # frames given to it, not back yet (in its order)
        self.made = 0
        self.dead = None
        self.closed = False             # its input is closed: it takes no more frames
        self.ended = False              # its output ended
        self.progress = 0.0             # when it last got a frame or gave one back
        self.stuck = False
        self.errors = collections.deque(maxlen=6)


class Join:
    """Hands frames to lanes, takes their packets back and yields them in frame order.

    A lane's muxer holds its last frame until its input ends, so every lane is closed when the
    take's frames run out; whatever a lane loses goes to another, or to a fresh CPU lane."""

    def __init__(self, lanes, frames, path, stop, spawn, stall_s=STALL_S):
        """frames: (offset, size) of each frame in the RAW at path, in order; spawn(kind) -> Lane."""
        self.lanes, self.frames, self.path, self.stop, self.spawn = lanes, frames, path, stop, spawn
        self.stall_s = stall_s
        self.cond = threading.Condition()
        self.pending = {}                # frame -> (offset, size), until a lane gave its packet back
        self.ready = {}                  # frame -> packet
        self.redo = []                   # frames of a dead lane, to give to another
        self.next_out = 0                # the next frame to write
        self.fed_all = False
        self.total = None
        self.ref = None                  # the lane whose header the joined stream uses
        self.error = None
        self.rescues = 0
        self.ref_ready = threading.Event()

    def start(self):
        for lane in self.lanes:
            self._run(lane)
        threading.Thread(target=self._feeder, daemon=True).start()

    def _run(self, lane):
        """One thread writing a lane's frames, one reading its packets, one draining its stderr."""
        for fn in (self._writer, self._reader, self._stderr):
            threading.Thread(target=fn, args=(lane,), daemon=True).start()

    def _writer(self, lane):
        """Reads the lane's frames from the RAW (each lane on its own) into its input."""
        src, out = FrameReader(self.path), lane.proc.stdin.fileno()
        try:
            while True:
                item = lane.inbox.get()
                if item is None:
                    break
                data = src.read(*item)
                while data:
                    data = data[os.write(out, data):]
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            src.close()
            try:
                lane.proc.stdin.close()
            except OSError:
                pass

    def _stderr(self, lane):
        for line in lane.proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text and "arm_release_ver" not in text:
                lane.errors.append(text)
                print(f"[{lane.kind}] {text}", file=sys.stderr, flush=True)

    def _reader(self, lane):
        why = None
        try:
            mkv = MkvStream(lane.proc.stdout).header()
            with self.cond:
                if self.ref is None:
                    self.ref = mkv
                    self.ref_ready.set()
                elif mkv.codec_private != self.ref.codec_private:
                    raise ValueError("its FFV1 settings differ from the other lane's")
            for packet in mkv.frames():
                with self.cond:
                    if not lane.fed:
                        raise ValueError("more frames than it was given")
                    n = lane.fed.popleft()
                    self.pending.pop(n, None)
                    self.ready[n] = packet
                    lane.made += 1
                    lane.progress = time.monotonic()
                    self.cond.notify_all()
        except (EOFError, ValueError, OSError) as e:
            why = str(e) or "it stopped"
        with self.cond:
            if lane.fed or (why and not lane.closed):
                lane.dead = (list(lane.errors) or [why or "it stopped"])[-1]
                self.redo.extend(lane.fed)
                lane.fed.clear()
                lane.inbox.put(None)
                try:
                    lane.proc.kill()
                except OSError:
                    pass
            lane.ended = True
            self.cond.notify_all()

    # -- under the lock
    def _watch(self):
        """The lane holding the oldest unfinished frame always gets frames while it has room; if
        it neither takes nor gives one for stall_s it is stuck: stopped, its frames go to another.
        (Any other lane may wait that long, legitimately: its muxer holds its last frame.)"""
        now = time.monotonic()
        for x in self.lanes:
            if (x.fed and x.fed[0] == self.next_out and not x.dead and not x.stuck
                    and now - x.progress > self.stall_s):
                x.stuck = True
                x.errors.append("no frame back for %d s" % self.stall_s)
                try:
                    x.proc.kill()
                except OSError:
                    pass

    def _rescue(self):
        """No lane takes frames any more: start a fresh CPU lane (at most RESCUES times)."""
        if self.rescues == RESCUES:
            self.error = "every encoder stopped: " + "; ".join(
                f"{x.kind}: {x.dead}" for x in self.lanes if x.dead)
            self.cond.notify_all()
            return False
        self.rescues += 1
        lane = self.spawn("cpu")
        self.lanes.append(lane)
        self._run(lane)
        return True

    def _pick(self, n):
        """The lane for frame n: every lane is kept full (each encodes on its own hardware), the
        emptiest first. Past the reorder window only the lane holding the oldest unfinished frame
        gets more - its muxer gives a frame back only when the next one comes in."""
        free = [x for x in self.lanes if not x.dead and not x.closed and len(x.fed) < x.depth]
        if n - self.next_out >= MAX_SPAN:
            free = [x for x in free if x.fed and x.fed[0] == self.next_out]
        return min(free, key=lambda x: (len(x.fed) / x.depth, -SPEED[x.kind])) if free else None

    # -- the feeder thread
    def _give(self, n, span, new=True):
        """Wait for room in a lane (and in the reorder window), then give it frame n: True.
        None when frames a lane lost turned up meanwhile - they go first (the oldest of them
        holds the window); False when the job stops."""
        with self.cond:
            while True:
                if self.stop.is_set() or self.error:
                    return False
                if new and self.redo:
                    return None
                if all(x.dead or x.closed for x in self.lanes) and not self._rescue():
                    return False
                lane = self._pick(n)
                if lane:
                    break
                self.cond.wait(0.5)
                self._watch()
            lane.progress = time.monotonic()
            lane.fed.append(n)
            self.pending[n] = span
        lane.inbox.put(span)
        return True

    def _give_back(self):
        """Give the frames dead lanes left to the lanes still open (oldest first)."""
        while True:
            with self.cond:
                if not self.redo:
                    return True
                self.redo.sort()
                n = self.redo.pop(0)
                span = self.pending[n]
            if not self._give(n, span, new=False):
                return False

    def _close(self):
        with self.cond:
            for lane in self.lanes:
                if not lane.closed:
                    lane.closed = True
                    lane.inbox.put(None)
            self.cond.notify_all()

    def _feeder(self):
        n = 0
        try:
            for span in self.frames:
                while True:
                    if not self._give_back():
                        return
                    given = self._give(n, span)
                    if given is not None:
                        break
                if not given:
                    return
                n += 1
            with self.cond:
                self.fed_all, self.total = True, n
            while True:
                if not self._give_back():
                    return
                self._close()           # each lane writes out what it holds, then ends
                with self.cond:
                    while not self.redo and not all(x.ended for x in self.lanes):
                        if self.stop.is_set() or self.error:
                            return
                        self.cond.wait(0.5)
                        self._watch()
                    if not self.redo:
                        return
        except (OSError, ValueError, KeyError) as e:
            with self.cond:
                self.error = self.error or str(e)
        finally:
            self._close()

    def packets(self):
        """Yields (frame number, packet) in order until every frame is written."""
        while True:
            with self.cond:
                while True:
                    if self.error:
                        raise RuntimeError(self.error)
                    if self.stop.is_set():
                        return
                    if self.next_out in self.ready:
                        packet = self.ready.pop(self.next_out)
                        break
                    if self.fed_all and self.next_out >= self.total:
                        return
                    self.cond.wait(0.5)
                n = self.next_out
                self.next_out += 1
                self.cond.notify_all()
            yield n, packet
