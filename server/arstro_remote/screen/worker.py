"""Screen capture worker (SCR-01, SCR-06): one monitor of the X desktop -> H.264 access units on stdout.

    python3 -m arstro_remote.screen.worker --quality medium [--encoder auto|vpu|cpu] [--bitrate BPS]
                                           [--monitor primary|all|<name>]

A separate process like the recorder worker (ARC-06): an X error or a GStreamer crash
ends only this process. Output on stdout, one record after another:

    b"F" [key u8][pts_us u64][size u32] <Annex-B access unit>
    b"C" [size u32] <JSON: {"width", "height", "fps", "bitrate", "quality", "encoder", "screen": [w, h],
                             "region": [x, y, w, h], "monitor", "monitors": [...], "desktop": [w, h]}>
    b"E" [size u32] <JSON: {"error"}>                    (then the worker exits)

stdin takes JSON lines: {"cmd": "keyframe"}.

Encoders (SCR-05): "vpu" is the Rockchip hardware encoder (mpph264enc), "cpu" is x264, "auto" takes
the VPU when GStreamer has it and falls back to x264 if it fails before the first frame.

Monitors (SCR-06): the capture covers one monitor of the desktop (`--monitor`: the primary one, a
monitor's RandR name, or `all` for the whole desktop); `screen` is the size of that area and `region`
its place on the desktop, so a controller adds region's x, y to a point of the picture. A monitor
that goes away gives the primary one; a change of the monitors restarts the capture. The VPU
keeps up only when (1) it gets NV12 from a videoconvert behind a queue (its own copy then runs on
another thread than the capture) and (2) zero-copy-pkt is on - without it each access unit costs a
fixed copy and the encoder tops out near 19 fps even at 1024x768; with it, 140 fps from a test
source and full rate from the desktop at a third of x264's CPU. max-reenc=0: its CBR would
otherwise re-encode frames that miss the rate.
"""

import argparse
import json
import os
import struct
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from .xdisplay import describe, pick  # noqa: E402

# quality -> (max width, fps, bits/s at 1280x720; scaled by the picture area)
QUALITIES = {"low": (960, 15, 800_000), "medium": (1280, 30, 2_500_000), "high": (0, 30, 5_000_000)}
ENCODERS = ("auto", "vpu", "cpu")
BITRATE_RANGE = (200_000, 20_000_000)                    # an explicit --bitrate, bits/s
FRAME = struct.Struct(">BQI")
SIZE = struct.Struct(">I")
_out_lock = threading.Lock()


def emit(kind, payload):
    with _out_lock:
        sys.stdout.buffer.write(kind + SIZE.pack(len(payload)) + payload)
        sys.stdout.buffer.flush()


def emit_json(kind, obj):
    emit(kind, json.dumps(obj).encode())


def has_idr(au):
    """Does an Annex-B access unit hold an IDR slice? mpph264enc flags every buffer DELTA_UNIT,
    so the keyframe mark has to come from the NAL types."""
    i = au.find(b"\x00\x00\x01")
    while 0 <= i < len(au) - 3:
        if au[i + 3] & 0x1F == 5:
            return True
        i = au.find(b"\x00\x00\x01", i + 3)
    return False


def fit(w, h, max_w):
    if max_w and w > max_w:
        h = round(h * max_w / w)
        w = max_w
    return w & ~1, h & ~1


class Worker:
    def __init__(self, display, quality, encoder="auto", bitrate=0, monitor="primary"):
        self.display = display
        self.monitor = monitor or "primary"
        self.quality = quality if quality in QUALITIES else "medium"
        self.encoder = encoder if encoder in ENCODERS else "auto"
        self.bitrate = min(max(bitrate, BITRATE_RANGE[0]), BITRATE_RANGE[1]) if bitrate else 0
        self.use = "cpu" if self.encoder == "cpu" or not Gst.ElementFactory.find("mpph264enc") else "vpu"
        if self.encoder == "vpu" and self.use == "cpu":
            print("screen: no mpph264enc in GStreamer - encoding with x264", file=sys.stderr)
        self.frames = 0
        self.loop = GLib.MainLoop()
        self.pipe = None
        self.desc = None                                 # xdisplay.describe(): the desktop and its monitors

    def build(self, desc):
        area = pick(desc, self.monitor)
        size = (area["width"], area["height"])
        whole = (area["x"], area["y"]) == (0, 0) and list(size) == desc["screen"]
        max_w, fps, bps720 = QUALITIES[self.quality]
        w, h = fit(size[0], size[1], max_w)
        bps = self.bitrate or int(bps720 * max(0.35, (w * h) / (1280 * 720)))
        key = max(1, fps // 2)                           # keyframe every 0.5 s: quick recovery
        if self.use == "vpu":
            enc = ("videoconvert ! video/x-raw,format=NV12 ! "
                   "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! "
                   f"mpph264enc name=enc rc-mode=cbr bps={bps} max-reenc=0 zero-copy-pkt=true gop={key} "
                   "header-mode=each-idr profile=main ! video/x-h264,stream-format=byte-stream,alignment=au ! ")
        else:
            enc = ("videoconvert ! video/x-raw,format=I420 ! "
                   f"x264enc name=enc tune=zerolatency speed-preset=ultrafast bitrate={bps // 1000} "
                   f"vbv-buf-capacity=400 key-int-max={key} byte-stream=true sliced-threads=false threads=2 ! "
                   "video/x-h264,stream-format=byte-stream,alignment=au,profile=main ! ")
        crop = "" if whole else (f"startx={area['x']} starty={area['y']} "        # end* are inclusive
                                 f"endx={area['x'] + size[0] - 1} endy={area['y'] + size[1] - 1} ")
        launch = (f"ximagesrc name=src display-name={self.display} use-damage=false show-pointer=true {crop}! "
                  f"video/x-raw,framerate={fps}/1 ! "
                  "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                  f"videoscale ! video/x-raw,width={w},height={h} ! " + enc +
                  "appsink name=sink emit-signals=true sync=false async=false max-buffers=4 drop=true")
        pipe = Gst.parse_launch(launch)
        pipe.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self.config = {"width": w, "height": h, "fps": fps, "bitrate": bps, "quality": self.quality,
                       "encoder": self.use, "screen": list(size),
                       "region": [area["x"], area["y"], size[0], size[1]], "monitor": area["name"],
                       "monitors": desc["monitors"], "desktop": desc["screen"]}
        return pipe

    def start(self, desc):
        self.stop()
        self.desc = desc
        self.frames = 0
        self.pipe = self.build(desc)
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
        self.frames += 1
        key = has_idr(data) if self.use == "vpu" else not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        pts = buf.pts // 1000 if buf.pts != Gst.CLOCK_TIME_NONE else 0
        with _out_lock:
            sys.stdout.buffer.write(b"F" + FRAME.pack(1 if key else 0, pts, len(data)) + data)
            sys.stdout.buffer.flush()
        return Gst.FlowReturn.OK

    def _on_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        if self.use == "vpu" and self.encoder == "auto" and not self.frames:
            print("screen: the VPU encoder failed (%s) - falling back to x264" % err.message, file=sys.stderr)
            self.use = "cpu"
            GLib.idle_add(lambda: self.start(self.desc) and False)
            return
        emit_json(b"E", {"error": "screen capture: %s" % err.message})
        self.loop.quit()

    def keyframe(self):
        if self.pipe:
            enc = self.pipe.get_by_name("enc")
            ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            enc.get_static_pad("src").send_event(ev)

    def _watch_size(self):
        desc = describe(self.display)
        if desc is None:
            emit_json(b"E", {"error": "the desktop (%s) went away" % self.display})
            self.loop.quit()
            return False
        if desc != self.desc:                           # xrandr change: other size / monitors
            self.start(desc)
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
        desc = describe(self.display)
        if desc is None:
            emit_json(b"E", {"error": "no desktop on %s (is the Pi logged in to its desktop?)" % self.display})
            return 3
        self.start(desc)
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
    ap.add_argument("--encoder", default="auto", choices=list(ENCODERS))
    ap.add_argument("--bitrate", type=int, default=0, help="bits/s; 0 = from the quality")
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    ap.add_argument("--monitor", default="primary", help="primary, all, or a monitor's name (xrandr --listmonitors)")
    a = ap.parse_args(argv)
    Gst.init(None)
    return Worker(a.display, a.quality, a.encoder, a.bitrate, a.monitor).run()


if __name__ == "__main__":
    sys.exit(main())
