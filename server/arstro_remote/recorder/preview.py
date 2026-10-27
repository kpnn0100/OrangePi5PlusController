"""Live preview fan-out (REC-02): one H.264 encoder in the worker, any number of viewers.

Viewer protocol (WebSocket /ws/preview):
  text    {"type": "config", "codec": "h264", "width", "height", "fps", "bitrate", "quality"}
          {"type": "state", "state": "starting" | "live" | "no-signal" | "stopped", "detail"}
  binary  [version u8 = 1][flags u8: bit0 keyframe][pts_us u64 BE][Annex-B access unit]

What a viewer may ask for (text from the viewer; an old viewer asks nothing and gets nothing new):
  {"type": "hello", "want": ["levels", "analysis"]}
      levels    text {"type": "levels", "peak": [dB, ...], "rms": [dB, ...]} 10 a second (REC-10)
      analysis  binary [version u8 = 2][kind u8 = 1][json length u16 BE][JSON][bytes], the
                exposure data of monitor.py, 4 a second while the settings show scopes,
                zebras or false colour (REC-13)
  {"type": "zoom", "on": true, "x": 0.5, "y": 0.5}   focus zoom around (x, y), 0..1 of the
      picture, at the settings' ratio (REC-14). The viewer gets {"type": "zoom", "on": true,
      "ratio", "x", "y", "width", "height", "src_width", "src_height", "crop": [l, t, w, h],
      "pixels"} and, when the size changes, a new "config"; from then on its video frames are
      the crop's. Moving sends only the new "zoom". {"type": "zoom", "on": false} (or an error:
      {"type": "zoom", "on": false, "error"}) brings the whole picture back with its "config".

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

from ..web.ws import OP_BINARY, OP_TEXT, WSClosed

log = logging.getLogger("arstro.preview")

LINGER = 5.0
# Live beats complete: a viewer more than ~0.25 s behind skips to the next keyframe
# (which is requested at once) instead of watching an ever older picture.
MAX_QUEUE = 8
SNDBUF = 64 * 1024              # small socket buffer: a slow link shows up in our queue, not in the kernel
HEAD = struct.Struct(">BBQ")


class Viewer:
    def __init__(self, conn, peer, vid=0):
        self.conn = conn
        self.peer = peer
        self.id = vid
        self.want = set()                 # "levels", "analysis"
        self.zoom = None                  # the zoom it is shown, or None
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
    def __init__(self, on_viewers, request_keyframe, name="preview", on_message=None, on_leave=None):
        self.name = name                          # for the log: "preview" (HDMI) / "screen"
        self.on_viewers = on_viewers              # (count) -> start/stop the encoder
        self.request_keyframe = request_keyframe
        self.on_message = on_message              # (viewer, message) -> zoom, hello (REC-13/14)
        self.on_leave = on_leave                  # (viewer) when it goes
        self._next_id = 0
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
            if v.zoom is not None:
                continue                          # it watches its crop (zoom_frame)
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

    # ------------------------------------------------ monitoring (REC-10, REC-13, REC-14)
    def viewer(self, vid):
        with self.lock:
            return next((v for v in self.viewers if v.id == vid), None)

    def wanting(self, what):
        with self.lock:
            return [v for v in self.viewers if what in v.want]

    def levels(self, peak, rms):
        text = json.dumps({"type": "levels", "peak": peak, "rms": rms})
        for v in self.wanting("levels"):
            v.push(("text", text))

    def analysis(self, meta, payload):
        js = json.dumps(meta).encode()
        msg = struct.pack(">BBH", 2, 1, len(js)) + js + payload
        for v in self.wanting("analysis"):
            if len(v.queue) < MAX_QUEUE // 2:     # a viewer behind gets its picture first
                v.push(("bin", msg))

    def zoom_frame(self, vid, key, pts_us, data, request_key):
        v = self.viewer(vid)
        if v is None or v.zoom is None:
            return
        if v.need_key and not key:
            return
        if len(v.queue) >= MAX_QUEUE:
            v.queue.clear()
            v.dropped += 1
            v.need_key = True
            request_key()
            return
        v.need_key = False
        v.push(("bin", HEAD.pack(1, 1 if key else 0, pts_us) + data))

    def zoom_state(self, vid, z):
        """The worker's zoom for a viewer: on (with its geometry) or off (whole picture back)."""
        v = self.viewer(vid)
        if v is None:
            return
        if z.get("on"):
            before = v.zoom
            v.zoom = z
            v.push(("text", json.dumps(dict(z, type="zoom"))))
            if not before or (before.get("width"), before.get("height")) != (z["width"], z["height"]):
                v.queue = collections.deque(i for i in v.queue if i[0] == "text")
                v.need_key = True
                v.push(("text", json.dumps({"type": "config", "codec": "h264", "width": z["width"],
                                            "height": z["height"], "fps": z.get("fps"), "zoom": True})))
        else:
            was = v.zoom
            v.zoom = None
            v.push(("text", json.dumps({"type": "zoom", "on": False, "error": z.get("error")})))
            if was is not None:
                v.queue = collections.deque(i for i in v.queue if i[0] == "text")
                v.need_key = True
                if self.config:
                    v.push(("text", json.dumps(self.config)))
                self._keyframe()

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
        with self.lock:
            self._next_id += 1
            vid = self._next_id
        v = Viewer(conn, peer, vid)
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
        log.info("%s viewer %s joined (%d watching)", self.name, peer, n)
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
            log.info("%s viewer %s left after %d frames (%d dropped bursts, %d watching)",
                     self.name, peer, v.sent, v.dropped, n)
            if self.on_leave:
                try:
                    self.on_leave(v)
                except Exception:
                    log.exception("%s viewer leave", self.name)
            if n == 0:
                self._schedule_stop()
            else:
                self.on_viewers(n)

    def _drain_incoming(self, v):
        """Read what the viewer asks for (hello, zoom); notice when it goes away."""
        try:
            while True:
                op, data = v.conn.recv_message()
                if op != OP_TEXT or len(data) > 4096:
                    continue
                try:
                    obj = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "hello":
                    v.want = {w for w in (obj.get("want") or []) if w in ("levels", "analysis")}
                if self.on_message:
                    try:
                        self.on_message(v, obj)
                    except Exception:
                        log.exception("%s viewer message", self.name)
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
