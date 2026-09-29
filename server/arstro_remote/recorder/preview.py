"""Live preview fan-out (REC-02): one H.264 encoder in the worker, any number of viewers.

Viewer protocol (WebSocket /ws/preview):
  text    {"type": "config", "codec": "h264", "width", "height", "fps", "bitrate", "quality"}
          {"type": "state", "state": "starting" | "live" | "no-signal" | "stopped", "detail"}
  binary  [version u8 = 1][flags u8: bit0 keyframe][pts_us u64 BE][Annex-B access unit]

Every viewer has a small queue. A viewer that falls behind (slow Wi-Fi) drops what is
queued and waits for the next keyframe instead of slowing anyone else down. A viewer
joining asks the encoder for a keyframe, so a picture appears within a moment. The
encoder runs only while there are viewers (+ LINGER seconds, REC-06).
"""

import collections
import json
import logging
import socket
import struct
import threading
import time

from ..web.ws import OP_BINARY, WSClosed

log = logging.getLogger("arstro.preview")

LINGER = 5.0
# Live beats complete: a viewer more than ~0.25 s behind skips to the next keyframe
# (which is requested at once) instead of watching an ever older picture.
MAX_QUEUE = 8
SNDBUF = 64 * 1024              # small socket buffer: a slow link shows up in our queue, not in the kernel
HEAD = struct.Struct(">BBQ")


class Viewer:
    def __init__(self, conn, peer):
        self.conn = conn
        self.peer = peer
        self.queue = collections.deque()
        self.wake = threading.Event()
        self.need_key = True
        self.closed = False
        self.sent = 0
        self.dropped = 0

    def push(self, item):
        self.queue.append(item)
        self.wake.set()


class Preview:
    def __init__(self, on_viewers, request_keyframe):
        self.on_viewers = on_viewers              # (count) -> start/stop the encoder
        self.request_keyframe = request_keyframe
        self.viewers = set()
        self.lock = threading.Lock()
        self.config = None
        self.state = {"type": "state", "state": "stopped"}
        self._stop_timer = None
        self._last_key_request = 0.0

    @property
    def count(self):
        with self.lock:
            return len(self.viewers)

    # ------------------------------------------------------- worker -> viewers
    def set_config(self, cfg):
        self.config = dict(cfg, type="config", codec="h264")
        self._broadcast_text(self.config)

    def set_state(self, state, detail=None):
        st = {"type": "state", "state": state, "detail": detail}
        if st != self.state:
            self.state = st
            self._broadcast_text(st)

    def _broadcast_text(self, obj):
        text = json.dumps(obj)
        with self.lock:
            viewers = list(self.viewers)
        for v in viewers:
            v.push(("text", text))

    def frame(self, key, pts_us, data):
        msg = HEAD.pack(1, 1 if key else 0, pts_us) + data
        want_key = False
        with self.lock:
            viewers = list(self.viewers)
        for v in viewers:
            if v.need_key and not key:
                continue
            if len(v.queue) >= MAX_QUEUE:
                v.queue.clear()
                v.dropped += 1
                v.need_key = True
                want_key = True
                continue
            v.need_key = False
            v.push(("bin", msg))
        if want_key:
            self._keyframe()
        if key and self.state.get("state") != "live":
            self.set_state("live")

    def _keyframe(self):
        now = time.monotonic()
        if now - self._last_key_request > 0.5:
            self._last_key_request = now
            try:
                self.request_keyframe()
            except Exception:
                pass

    # ------------------------------------------------------------- viewers
    def serve(self, conn, peer):
        """Run one viewer until it disconnects (called in the HTTP handler thread)."""
        v = Viewer(conn, peer)
        try:
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
        except (OSError, AttributeError):
            pass
        with self.lock:
            self.viewers.add(v)
            n = len(self.viewers)
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
        log.info("preview viewer %s joined (%d watching)", peer, n)
        if self.config:
            v.push(("text", json.dumps(self.config)))
        v.push(("text", json.dumps(self.state)))
        self.on_viewers(n)
        self._keyframe()
        threading.Thread(target=self._drain_incoming, args=(v,), daemon=True).start()
        try:
            while not v.closed:
                v.wake.wait(2.0)
                v.wake.clear()
                while v.queue and not v.closed:
                    kind, payload = v.queue.popleft()
                    if kind == "text":
                        conn.send_text(payload)
                    else:
                        conn._send(OP_BINARY, payload)
                        v.sent += 1
        except (WSClosed, OSError):
            pass
        finally:
            v.closed = True
            conn.close()
            with self.lock:
                self.viewers.discard(v)
                n = len(self.viewers)
            log.info("preview viewer %s left after %d frames (%d dropped bursts, %d watching)",
                     peer, v.sent, v.dropped, n)
            if n == 0:
                self._schedule_stop()
            else:
                self.on_viewers(n)

    def _drain_incoming(self, v):
        """Read (and ignore) what the viewer sends; notice when it goes away."""
        try:
            while True:
                v.conn.recv_message()
        except (WSClosed, OSError):
            pass
        v.closed = True
        v.wake.set()

    def _schedule_stop(self):
        def stop():
            with self.lock:
                self._stop_timer = None
                if self.viewers:
                    return
            self.on_viewers(0)
        with self.lock:
            if self._stop_timer:
                self._stop_timer.cancel()
            self._stop_timer = threading.Timer(LINGER, stop)
            self._stop_timer.daemon = True
            self._stop_timer.start()

    def close_all(self):
        with self.lock:
            viewers = list(self.viewers)
        for v in viewers:
            v.closed = True
            v.wake.set()
