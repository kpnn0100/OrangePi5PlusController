#!/usr/bin/env python3
"""HDMI RX viewer for RK3588 (Orange Pi 5 Plus).

Advertises a 4K60 EDID on the HDMI RX port so a camera treats the board as a
3840x2160@60 monitor, then shows the incoming signal full screen with zero-copy
(dmabuf -> hardware display plane). Automatically restarts on hot-plug or
resolution change.

Keys: F / F11 toggle fullscreen, Q / Esc quit.
"""
import argparse
import glob
import os
import re
import subprocess
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

EDID_TYPES = {
    "4k60": "hdmi-4k-600mhz",   # up to 3840x2160@60 (600 MHz TMDS, HDMI 2.0)
    "4k30": "hdmi-4k-300mhz",   # up to 3840x2160@30
    "1080p": "hdmi",            # up to 1920x1080@60
}
POLL_MS = 1000
STABLE_POLLS = 2  # identical timing readings required before starting a stream


# ---------------------------------------------------------------- v4l2 helpers

def v4l2(dev, *args):
    r = subprocess.run(["v4l2-ctl", "-d", dev, *args], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def find_hdmirx_device():
    for path in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            with open(os.path.join(path, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if "hdmirx" in name.lower():
            return "/dev/" + os.path.basename(path)
    return None


def set_edid(dev, edid, edid_file):
    if edid_file:
        args = [f"--set-edid=pad=0,file={edid_file}"]
        label = edid_file
    else:
        args = [f"--set-edid=pad=0,type={EDID_TYPES[edid]}"]
        label = EDID_TYPES[edid]
    rc, out = v4l2(dev, *args)
    if rc != 0 or "failed" in out.lower():
        print(f"[warn] could not set EDID ({label}): {out.strip()}")
    else:
        print(f"[info] EDID set: {label} (source will re-read it via hot-plug)")


def query_signal(dev):
    """Return (width, height, fps) of the incoming signal, or None."""
    rc, out = v4l2(dev, "--query-dv-timings")
    w = re.search(r"Active width:\s*(\d+)", out)
    h = re.search(r"Active height:\s*(\d+)", out)
    if rc != 0 or not w or not h or int(w.group(1)) == 0:
        return None
    fps = re.search(r"\(([\d.]+) frames per second\)", out)
    return int(w.group(1)), int(h.group(1)), float(fps.group(1)) if fps else 0.0


def lock_timings(dev):
    v4l2(dev, "--set-dv-bt-timings=query")
    _, out = v4l2(dev, "--get-fmt-video")
    m = re.search(r"Pixel Format\s*:\s*'(\w+)'", out)
    return m.group(1) if m else "?"


def find_overlay_plane():
    """Pick a DRM overlay plane that can scan out NV12 on an active CRTC.

    rkximagesink's own plane search fails when X already holds the planes it
    looks at, so choose one ourselves: a YUV-capable plane allowed on a CRTC
    that is showing a mode, excluding that CRTC's primary (X desktop) plane.
    """
    try:
        out = subprocess.run(["modetest", "-M", "rockchip", "-p"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.splitlines()
    crtcs, planes, section = [], [], None
    for i, line in enumerate(lines):
        if line.startswith("CRTCs:"):
            section = "crtc"
        elif line.startswith("Planes:"):
            section = "plane"
        elif section == "crtc":
            m = re.match(r"(\d+)\s+(\d+)\s+\(\d+,\d+\)\s+\((\d+)x(\d+)\)", line)
            if m:
                crtcs.append((int(m.group(2)), int(m.group(3)) > 0))  # (fb, active)
        elif section == "plane":
            m = re.match(r"(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+\S+\s+\d+\s+0x([0-9a-f]+)", line)
            if m and i + 1 < len(lines) and "NV12" in lines[i + 1]:
                planes.append((int(m.group(1)), int(m.group(3)), int(m.group(4), 16)))
    for idx, (crtc_fb, active) in enumerate(crtcs):
        if not active:
            continue
        for plane_id, fb, mask in planes:
            if mask & (1 << idx) and (fb == 0 or fb != crtc_fb):
                return plane_id
    return None


def find_hdmiin_audio():
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                m = re.match(r"\s*\d+\s+\[(\S+)\s*\]", line)
                if m and "hdmiin" in m.group(1).lower():
                    return m.group(1)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------- viewer

class Viewer:
    def __init__(self, args, window=None):
        self.args = args
        self.dev = args.device
        self.window = window          # Gtk UI wrapper, or None for headless KMS
        self.pipeline = None
        self.audio = None
        self.current = None           # timing currently being streamed
        self.candidate = None
        self.candidate_count = 0
        self.sink = self._pick_sink()
        self.audio_card = None if args.no_audio else find_hdmiin_audio()
        self.plane_id = args.plane_id
        if self.sink == "rkximagesink" and self.plane_id is None:
            self.plane_id = find_overlay_plane()
        print(f"[info] device={self.dev} sink={self.sink} plane={self.plane_id} "
              f"audio={self.audio_card or 'off'}")

    def _pick_sink(self):
        if self.args.sink != "auto":
            return self.args.sink
        if os.environ.get("DISPLAY"):
            return "rkximagesink" if Gst.ElementFactory.find("rkximagesink") else "xvimagesink"
        return "kmssink"

    # -- pipeline control

    def _video_desc(self):
        io_mode = "dmabuf" if self.sink in ("rkximagesink", "kmssink") else "mmap"
        convert = "" if self.sink in ("rkximagesink", "kmssink") else "videoconvert ! "
        extra = " force-aspect-ratio=true" if self.sink != "kmssink" else ""
        if self.plane_id is not None and self.sink in ("rkximagesink", "kmssink"):
            extra += f" plane-id={self.plane_id}"
        return (f"v4l2src device={self.dev} io-mode={io_mode} do-timestamp=true ! "
                f"queue max-size-buffers=2 leaky=downstream ! {convert}"
                f"{self.sink} name=sink sync=false{extra}")

    def start(self, timing):
        fmt = lock_timings(self.dev)
        w, h, fps = timing
        info = f"{w}x{h} @ {fps:.2f} Hz  {fmt}"
        print(f"[info] signal: {info}")

        self.pipeline = Gst.parse_launch(self._video_desc())
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        if self.window:
            bus.enable_sync_message_emission()
            bus.connect("sync-message::element", self.window.on_sync_message)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            print("[error] failed to start video pipeline")
            self.stop()
            return
        self.current = timing
        if self.window:
            self.window.show_status(None, info)
        self._start_audio()

    def _start_audio(self):
        if not self.audio_card:
            return
        desc = (f"alsasrc device=plughw:CARD={self.audio_card},DEV=0 ! "
                "audio/x-raw,rate=48000,channels=2 ! "
                "queue max-size-time=200000000 leaky=downstream ! "
                "audioconvert ! audioresample ! autoaudiosink sync=false")
        try:
            self.audio = Gst.parse_launch(desc)
            self.audio.get_bus().add_signal_watch()
            self.audio.get_bus().connect("message::error", self._on_audio_error)
            self.audio.set_state(Gst.State.PLAYING)
        except GLib.Error as e:
            print(f"[warn] audio disabled: {e}")
            self.audio = None

    def stop(self):
        for p in (self.pipeline, self.audio):
            if p:
                p.set_state(Gst.State.NULL)
                p.get_bus().remove_signal_watch()
        self.pipeline = self.audio = None
        self.current = None

    # -- signal monitoring

    def poll(self):
        timing = query_signal(self.dev)

        if timing == self.current and self.pipeline:
            return True

        if timing is None:
            if self.pipeline:
                print("[info] signal lost")
                self.stop()
            self.candidate, self.candidate_count = None, 0
            if self.window:
                self.window.show_status("No HDMI signal\nwaiting for source (4K60 EDID advertised)…", None)
            return True

        # new or changed timing: wait until it is stable before (re)starting
        if timing != self.candidate:
            self.candidate, self.candidate_count = timing, 1
            return True
        self.candidate_count += 1
        if self.candidate_count >= STABLE_POLLS:
            self.stop()
            self.start(timing)
        return True

    def _on_message(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[error] video: {err.message}\n        {dbg}")
            self.stop()  # poll() restarts when the signal is stable again
            self.candidate, self.candidate_count = None, 0
        elif msg.type == Gst.MessageType.EOS:
            self.stop()

    def _on_audio_error(self, bus, msg):
        err, _ = msg.parse_error()
        print(f"[warn] audio: {err.message}")
        if self.audio:
            self.audio.set_state(Gst.State.NULL)
            self.audio = None


# ---------------------------------------------------------------- GTK window

class Window:
    def __init__(self, fullscreen, on_quit):
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GstVideo", "1.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import Gdk, GdkX11, GstVideo, Gtk  # noqa: F401
        self.Gtk, self.Gdk, self.GstVideo = Gtk, Gdk, GstVideo

        self.win = Gtk.Window(title="HDMI RX")
        self.win.set_default_size(1280, 720)
        self.win.connect("destroy", lambda *_: on_quit())
        self.win.connect("key-press-event", self._on_key)

        css = Gtk.CssProvider()
        css.load_from_data(b"window, .video { background: black; }"
                           b" .status { color: #bbb; font-size: 22px; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        overlay = Gtk.Overlay()
        self.area = Gtk.DrawingArea()
        self.area.get_style_context().add_class("video")
        self.area.connect("draw", self._on_draw)
        overlay.add(self.area)
        self.label = Gtk.Label(justify=Gtk.Justification.CENTER)
        self.label.get_style_context().add_class("status")
        overlay.add_overlay(self.label)
        self.win.add(overlay)

        self.win.show_all()
        self.xid = self.area.get_window().get_xid()
        self.is_fullscreen = False
        if fullscreen:
            self.toggle_fullscreen()
        self.show_status("Starting…", None)

    def _on_draw(self, widget, cr):
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        return False

    def _on_key(self, widget, event):
        key = self.Gdk.keyval_name(event.keyval).lower()
        if key in ("f", "f11"):
            self.toggle_fullscreen()
        elif key in ("q", "escape"):
            self.win.destroy()

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.win.unfullscreen()
        else:
            self.win.fullscreen()
        self.is_fullscreen = not self.is_fullscreen

    def show_status(self, text, title_info):
        self.label.set_visible(bool(text))
        self.label.set_text(text or "")
        self.win.set_title(f"HDMI RX — {title_info}" if title_info else "HDMI RX — no signal")
        self.area.queue_draw()

    def on_sync_message(self, bus, msg):
        # Called from the streaming thread: embed the sink in our window.
        if self.GstVideo.is_video_overlay_prepare_window_handle_message(msg):
            msg.src.set_window_handle(self.xid)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Show RK3588 HDMI RX input on screen.")
    ap.add_argument("-d", "--device", help="V4L2 device (default: auto-detect hdmirx)")
    ap.add_argument("--edid", choices=[*EDID_TYPES, "keep"], default="4k60",
                    help="EDID to advertise to the source (default: 4k60)")
    ap.add_argument("--edid-file", help="custom EDID file (hex, as from v4l2-ctl --get-edid)")
    ap.add_argument("--sink", default="auto",
                    choices=["auto", "rkximagesink", "kmssink", "xvimagesink", "glimagesink"])
    ap.add_argument("--plane-id", type=int, help="DRM plane for the video (default: auto)")
    ap.add_argument("--windowed", action="store_true", help="start windowed instead of fullscreen")
    ap.add_argument("--no-audio", action="store_true", help="do not play HDMI RX audio")
    args = ap.parse_args()

    Gst.init(None)

    args.device = args.device or find_hdmirx_device()
    if not args.device:
        sys.exit("HDMI RX device not found. Enable it first: sudo ./setup_hdmirx.sh && sudo reboot")

    if args.edid != "keep" or args.edid_file:
        set_edid(args.device, args.edid, args.edid_file)

    loop = GLib.MainLoop()
    viewer = None

    def quit_app():
        if viewer:
            viewer.stop()
        loop.quit()

    use_gui = bool(os.environ.get("DISPLAY")) and args.sink != "kmssink"
    window = Window(not args.windowed, quit_app) if use_gui else None
    viewer = Viewer(args, window)

    viewer.poll()
    GLib.timeout_add(POLL_MS, viewer.poll)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        viewer.stop()


if __name__ == "__main__":
    main()
