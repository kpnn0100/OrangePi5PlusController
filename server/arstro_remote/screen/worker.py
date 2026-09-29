"""Screen capture worker (SCR-01): the X desktop -> H.264 access units on stdout.

    python3 -m arstro_remote.screen.worker --quality medium

A separate process like the recorder worker (ARC-06): an X error or a GStreamer crash
ends only this process. Output on stdout, one record after another:

    b"F" [key u8][pts_us u64][size u32] <Annex-B access unit>
    b"C" [size u32] <JSON: {"width", "height", "fps", "bitrate", "quality", "screen": [w, h]}>
    b"E" [size u32] <JSON: {"error"}>                    (then the worker exits)

stdin takes JSON lines: {"cmd": "keyframe"}.
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import struct
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

# quality -> (max width, fps, bits/s at 1280x720; scaled by the picture area)
QUALITIES = {"low": (960, 15, 800_000), "medium": (1280, 30, 2_500_000), "high": (0, 30, 5_000_000)}
FRAME = struct.Struct(">BQI")
SIZE = struct.Struct(">I")
_out_lock = threading.Lock()


def emit(kind, payload):
    with _out_lock:
        sys.stdout.buffer.write(kind + SIZE.pack(len(payload)) + payload)
        sys.stdout.buffer.flush()


def emit_json(kind, obj):
    emit(kind, json.dumps(obj).encode())


def screen_size(display):
    """(width, height) of the X screen, or None when there is no desktop."""
    lib = ctypes.util.find_library("X11")
    if not lib:
        return None
    x = ctypes.CDLL(lib)
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x.XDisplayWidth.argtypes = x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x.XOpenDisplay(display.encode() if display else None)
    if not dpy:
        return None
    try:
        s = x.XDefaultScreen(dpy)
        return x.XDisplayWidth(dpy, s), x.XDisplayHeight(dpy, s)
    finally:
        x.XCloseDisplay(dpy)


def fit(w, h, max_w):
    if max_w and w > max_w:
        h = round(h * max_w / w)
        w = max_w
    return w & ~1, h & ~1


class Worker:
    def __init__(self, display, quality):
        self.display = display
        self.quality = quality if quality in QUALITIES else "medium"
        self.loop = GLib.MainLoop()
        self.pipe = None
        self.size = None

    def build(self, size):
        max_w, fps, bps720 = QUALITIES[self.quality]
        w, h = fit(size[0], size[1], max_w)
        bps = int(bps720 * max(0.35, (w * h) / (1280 * 720)))
        key = max(1, fps // 2)                           # keyframe every 0.5 s: quick recovery
        desc = (f"ximagesrc name=src display-name={self.display} use-damage=false show-pointer=true ! "
                f"video/x-raw,framerate={fps}/1 ! "
                "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                f"videoscale ! video/x-raw,width={w},height={h} ! videoconvert ! video/x-raw,format=I420 ! "
                f"x264enc name=enc tune=zerolatency speed-preset=ultrafast bitrate={bps // 1000} "
                f"vbv-buf-capacity=400 key-int-max={key} byte-stream=true sliced-threads=false threads=2 ! "
                "video/x-h264,stream-format=byte-stream,alignment=au,profile=main ! "
                "appsink name=sink emit-signals=true sync=false async=false max-buffers=4 drop=true")
        pipe = Gst.parse_launch(desc)
        pipe.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self.config = {"width": w, "height": h, "fps": fps, "bitrate": bps, "quality": self.quality,
                       "screen": list(size)}
        return pipe

    def start(self, size):
        self.stop()
        self.size = size
        self.pipe = self.build(size)
        self.pipe.set_state(Gst.State.PLAYING)
        emit_json(b"C", self.config)

    def stop(self):
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        pts = buf.pts // 1000 if buf.pts != Gst.CLOCK_TIME_NONE else 0
        with _out_lock:
            sys.stdout.buffer.write(b"F" + FRAME.pack(1 if key else 0, pts, len(data)) + data)
            sys.stdout.buffer.flush()
        return Gst.FlowReturn.OK

    def _on_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        emit_json(b"E", {"error": "screen capture: %s" % err.message})
        self.loop.quit()

    def keyframe(self):
        if self.pipe:
            enc = self.pipe.get_by_name("enc")
            ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            enc.get_static_pad("src").send_event(ev)

    def _watch_size(self):
        size = screen_size(self.display)
        if size is None:
            emit_json(b"E", {"error": "the desktop (%s) went away" % self.display})
            self.loop.quit()
            return False
        if size != self.size:                           # xrandr change: new picture size
            self.start(size)
        return True

    def _stdin(self):
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            if cmd.get("cmd") == "keyframe":
                GLib.idle_add(lambda: self.keyframe() and False)
        GLib.idle_add(self.loop.quit)                   # the daemon went away

    def run(self):
        size = screen_size(self.display)
        if size is None:
            emit_json(b"E", {"error": "no desktop on %s (is the Pi logged in to its desktop?)" % self.display})
            return 3
        self.start(size)
        GLib.timeout_add(2000, self._watch_size)
        threading.Thread(target=self._stdin, daemon=True).start()
        try:
            self.loop.run()
        finally:
            self.stop()
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arstro_remote.screen.worker")
    ap.add_argument("--quality", default="medium", choices=list(QUALITIES))
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    a = ap.parse_args(argv)
    Gst.init(None)
    return Worker(a.display, a.quality).run()


if __name__ == "__main__":
    sys.exit(main())
