"""Touch-screen recorder UI (GTK 3).

Layout (the video is on a hardware overlay plane that sits *above* the X
desktop, so no widget may ever be drawn on top of it — controls live around it):

    ┌──────────────────────── top bar: signal · timecode · storage ─────┬────────┐
    │                                                  │ settings panel │  MODE  │
    │                   live video                     │ (slides in,    │        │
    │           (tap to hide / show controls)          │  video shrinks)│   ◉    │
    │                                                  │                │  JOBS  │
    ├──────────────────────── bottom bar: stats · messages ─────────────┤  SETUP │
    └───────────────────────────────────────────────────────────────────┴────────┘
"""
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkX11, GLib, Gst, GstVideo, Gtk, Pango  # noqa: E402,F401

from . import arh, gpu, jobs, library, log, player, settings as settings_mod, v4l2  # noqa: E402
from .capture import Capture  # noqa: E402

CSS = b"""
* { font-family: "Roboto", "DejaVu Sans", sans-serif; }
window, .root { background-color: #000; color: #f2f2f7; }
.bar { background-color: #111114; padding: 4px 18px; min-height: 52px; }
.sidebar { background-color: #111114; padding: 12px 10px; }
.panel { background-color: #1a1a1d; border-left: 1px solid #2c2c30; }
.panel-body { padding: 8px 22px 28px 22px; }
.panel-title { font-size: 26px; font-weight: bold; }
.section { font-size: 14px; font-weight: bold; color: #8e8e93; margin-top: 22px; margin-bottom: 4px; }
.note { font-size: 15px; color: #8e8e93; }
.warn { color: #ff9f0a; }
.big { font-size: 20px; }
.signal { font-size: 19px; }
.signal-on { color: #30d158; }
.signal-off { color: #8e8e93; }
.tc { font-family: "DejaVu Sans Mono", monospace; font-size: 32px; font-weight: bold; }
.tc-rec { color: #ff453a; }
.stats { font-size: 16px; color: #aeaeb2; }
.alert { color: #ff453a; font-weight: bold; }
.toast { font-size: 17px; color: #ffd60a; }
.status { font-size: 26px; color: #aeaeb2; }

button {
  min-height: 58px; min-width: 58px; padding: 4px 14px;
  font-size: 18px; color: #f2f2f7;
  background-image: none; background-color: #2c2c2e;
  border: none; border-radius: 12px; box-shadow: none; text-shadow: none;
}
button:hover { background-color: #3a3a3c; }
button:active { background-color: #48484a; }
button:checked { background-color: #0a84ff; color: #fff; }
button:disabled { color: #5a5a5e; background-color: #1f1f21; }
.side-btn { min-height: 86px; font-size: 16px; font-weight: bold; }
.exit-btn { min-height: 64px; font-size: 16px; font-weight: bold; }
.side-btn:checked { background-color: #0a84ff; }
.recbtn, .recbtn:hover, .recbtn:active, .recbtn:checked {
  background-color: transparent; padding: 0; min-height: 0; min-width: 0;
}
.card { padding: 14px 18px; border-radius: 16px; background-color: #232326; }
.card:checked { background-color: #0b3d75; box-shadow: inset 0 0 0 3px #0a84ff; }
.card-title { font-size: 21px; font-weight: bold; }
.card-sub { font-size: 15px; color: #aeaeb2; }
.card:checked .card-sub { color: #d0e4ff; }
.seg button { border-radius: 0; margin: 0; min-width: 64px; }
.seg button:first-child { border-radius: 12px 0 0 12px; }
.seg button:last-child { border-radius: 0 12px 12px 0; }
.danger { background-color: #5c1a17; }
.danger:hover { background-color: #7a221e; }
.job { background-color: #232326; border-radius: 14px; padding: 12px 16px; margin-top: 10px; }
.job-title { font-size: 18px; font-weight: bold; }
progressbar trough { min-height: 12px; border-radius: 6px; background-color: #3a3a3c; border: none; }
progressbar progress { min-height: 12px; border-radius: 6px; background-color: #0a84ff; border: none; }
switch { min-width: 96px; min-height: 46px; border-radius: 23px; background-color: #3a3a3c; border: none; }
switch:checked { background-color: #30d158; }
switch slider { min-width: 44px; min-height: 44px; border-radius: 22px; background-color: #fff; border: none; }
scrollbar slider { min-width: 8px; }
.gitem { padding: 8px 12px; min-height: 64px; }
.badge { font-weight: bold; font-size: 15px; padding: 4px 10px; border-radius: 8px;
  color: #fff; min-width: 64px; }
.badge-raw { background-color: #ff9f0a; color: #111; }
.badge-h265 { background-color: #0a84ff; }
.badge-ffv1 { background-color: #bf5af2; }
.badge-other { background-color: #636366; }
.clip-title { font-size: 17px; font-weight: bold; }
.enc button { min-height: 48px; font-size: 15px; padding: 2px 8px; }
.playbar { background-color: #111114; padding: 6px 12px; }
.playbar button { min-width: 84px; }
scale trough { min-height: 12px; border-radius: 6px; }
scale slider { min-width: 34px; min-height: 34px; border-radius: 17px; background-color: #fff; }
scale highlight { background-color: #0a84ff; border-radius: 6px; }
"""
BADGE = {"RAW": "badge-raw", "H.265": "badge-h265", "FFV1": "badge-ffv1"}

H265_BITRATES = [20, 40, 60, 80, 120, 160]
QUALITIES = [("high", "High"), ("higher", "Higher"), ("max", "Max")]
X265_SPEEDS = [("ultrafast", "Ultrafast"), ("veryfast", "Very fast"), ("fast", "Fast"),
               ("medium", "Medium")]
FORMAT_BPP = {"NV12": 1.5, "NV21": 1.5, "NV16": 2, "NV61": 2, "YUY2": 2, "UYVY": 2}


# ---------------------------------------------------------------- formatting

def fmt_bytes(n):
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n / 1e3:.0f} kB"


def fmt_duration(s):
    s = int(s)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    if s >= 60:
        return f"{s // 60} min"
    return f"{s} s"


def short_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + "/") else p


def fmt_tc(s):
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- widgets

def label(text="", cls=None, xalign=0.0, wrap=False):
    lb = Gtk.Label(label=text, xalign=xalign)
    if wrap:
        lb.set_line_wrap(True)
        lb.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)   # also breaks long paths
        lb.set_max_width_chars(40)
    for c in (cls or "").split():
        lb.get_style_context().add_class(c)
    return lb


def add_class(w, *classes):
    for c in classes:
        w.get_style_context().add_class(c)
    return w


class Segmented(Gtk.Box):
    """Row of big mutually exclusive toggle buttons."""

    def __init__(self, options, value, on_change):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        add_class(self, "seg")
        self.on_change = on_change
        self.buttons = {}
        self._guard = False
        for val, text in options:
            b = Gtk.ToggleButton(label=text)
            b.connect("toggled", self._toggled, val)
            self.pack_start(b, True, True, 0)
            self.buttons[val] = b
        self.set_value(value)

    def set_value(self, value):
        self._guard = True
        for val, b in self.buttons.items():
            b.set_active(val == value)
        self._guard = False
        self.value = value

    def _toggled(self, button, val):
        if self._guard:
            return
        if not button.get_active():            # keep one selected
            self._guard = True
            button.set_active(True)
            self._guard = False
            return
        self.set_value(val)
        self.on_change(val)

    def set_option_sensitive(self, val, sensitive):
        self.buttons[val].set_sensitive(sensitive)


class Card(Gtk.ToggleButton):
    def __init__(self, title, sub):
        super().__init__()
        add_class(self, "card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.title = label(title, "card-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.title.set_max_width_chars(28)
        self.sub = label(sub, "card-sub", wrap=True)
        box.pack_start(self.title, False, False, 0)
        box.pack_start(self.sub, False, False, 0)
        self.add(box)


def switch_row(text, active, on_change, sub=None):
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    col.pack_start(label(text, "big"), False, False, 0)
    sub_label = None
    if sub:
        sub_label = label(sub, "note", wrap=True)
        col.pack_start(sub_label, False, False, 0)
    row.pack_start(col, True, True, 0)
    sw = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
    sw.connect("notify::active", lambda s, _p: on_change(s.get_active()))
    row.pack_end(sw, False, False, 0)
    row.switch, row.sub = sw, sub_label
    return row


class RecordButton(Gtk.Button):
    """Camera-style shutter: red disc (idle) -> red rounded square (recording)."""

    SIZE = 128

    def __init__(self):
        super().__init__()
        add_class(self, "recbtn")
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.area = Gtk.DrawingArea()
        self.area.set_size_request(self.SIZE, self.SIZE)
        self.area.connect("draw", self._draw)
        self.add(self.area)
        self.state = "disabled"                # disabled | idle | recording | busy
        self.pressed = False
        self.connect("pressed", lambda *_: self._press(True))
        self.connect("released", lambda *_: self._press(False))

    def _press(self, down):
        self.pressed = down
        self.area.queue_draw()

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.area.queue_draw()

    def _draw(self, w, cr):
        a = w.get_allocation()
        cx, cy = a.width / 2, a.height / 2
        r = min(cx, cy) - 4
        # outer ring
        cr.set_line_width(7)
        cr.set_source_rgb(0.95, 0.95, 0.97) if self.state != "disabled" else \
            cr.set_source_rgb(0.35, 0.35, 0.37)
        cr.arc(cx, cy, r - 3.5, 0, 2 * math.pi)
        cr.stroke()
        red = (1.0, 0.27, 0.23)
        if self.state == "disabled":
            cr.set_source_rgb(0.30, 0.30, 0.32)
        elif self.state == "busy":
            cr.set_source_rgb(0.6, 0.6, 0.62)
        else:
            cr.set_source_rgb(*red)
        scale = 0.92 if self.pressed else 1.0
        if self.state in ("recording", "busy"):
            s = r * 0.78 * scale
            rad = s * 0.18
            x, y = cx - s / 2, cy - s / 2
            cr.new_sub_path()
            cr.arc(x + s - rad, y + rad, rad, -math.pi / 2, 0)
            cr.arc(x + s - rad, y + s - rad, rad, 0, math.pi / 2)
            cr.arc(x + rad, y + s - rad, rad, math.pi / 2, math.pi)
            cr.arc(x + rad, y + rad, rad, math.pi, 3 * math.pi / 2)
            cr.close_path()
            cr.fill()
        else:
            cr.arc(cx, cy, (r - 14) * scale, 0, 2 * math.pi)
            cr.fill()
        return False


# ---------------------------------------------------------------- application

class App:
    def __init__(self, args, main_script):
        self.args = args
        self.s = settings_mod.load()
        if args.edid:
            self.s["edid"] = args.edid
        self.has_vpu = bool(Gst.ElementFactory.find("mpph265enc"))
        self.has_ffv1 = bool(Gst.ElementFactory.find("avenc_ffv1"))
        self.has_gpu_ffv1 = gpu.available()
        self.has_x265 = bool(Gst.ElementFactory.find("x265enc"))
        if not self.has_vpu and self.s["mode"] == "h265":
            self.s["mode"] = "raw"
        self.loop = GLib.MainLoop()
        self.quit_armed = 0.0
        self.rec_done = set()
        self.last_toast = 0.0
        self.chrome_visible = True
        self.player = None

        self.sim = v4l2.SimulatedSignal(args.simulate) if args.simulate else None
        self.dev = None if self.sim else (args.device or v4l2.find_hdmirx_device())
        self.main_script = main_script
        self.jobs = jobs.JobManager(main_script, self._on_job_update)
        self._build_window()

        if not self.sim and not self.dev:
            self.status.set_text("HDMI RX device not found.\n"
                                 "Enable it once:  sudo ./setup_hdmirx.sh  then reboot.")
            self.capture = None
            return
        if not self.sim:
            self._apply_edid()
        plane = args.plane_id
        if plane is None and args.sink == "rkximagesink":
            plane = v4l2.find_overlay_plane()
        audio_card = None if self.sim else v4l2.find_hdmiin_audio()
        self.capture = Capture(self.dev, self, sink=args.sink, plane_id=plane,
                               audio_card=audio_card, monitor_audio=self.s["audio"]["monitor"],
                               simulate=self.sim)
        print(f"[info] device={self.dev or 'simulated'} plane={plane} "
              f"audio={audio_card or ('test tone' if self.sim else 'off')} "
              f"vpu={self.has_vpu} ffv1={self.has_ffv1}")
        self._layout_video()
        self.capture.poll()
        GLib.timeout_add(1000, self._poll)
        GLib.idle_add(self._repair_interrupted)

    def _apply_edid(self):
        e = self.s["edid"]
        if self.args.edid_file:
            v4l2.set_edid(self.dev, None, self.args.edid_file)
        elif e.startswith("file:"):
            if os.path.exists(e[5:]):
                v4l2.set_edid(self.dev, None, e[5:])
            else:
                print(f"[warn] saved EDID {e[5:]} is missing, using 4K60")
                v4l2.set_edid(self.dev, "4k60")
        elif e in v4l2.EDID_TYPES:
            v4l2.set_edid(self.dev, e)

    # ------------------------------------------------------------ window

    def _build_window(self):
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", True)

        self.win = Gtk.Window(title="HDMI Recorder")
        add_class(self.win, "root")
        self.win.set_default_size(1280, 720)
        self.win.connect("delete-event", lambda *_: self.quit() or True)
        self.win.connect("key-press-event", self._on_key)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(main, True, True, 0)

        # top bar
        self.topbar = add_class(Gtk.Box(spacing=16), "bar")
        self.sig_label = label("● No signal", "signal signal-off")
        self.tc_label = label("00:00:00", "tc", xalign=0.5)
        self.store_label = label("", "stats", xalign=1.0)
        # long status texts must shorten, not push the bar wider than the screen
        self.sig_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.store_label.set_ellipsize(Pango.EllipsizeMode.START)
        self.sig_label.set_width_chars(10)
        self.store_label.set_width_chars(10)
        self.topbar.pack_start(self.sig_label, True, True, 0)
        self.topbar.set_center_widget(self.tc_label)
        self.topbar.pack_end(self.store_label, True, True, 0)
        main.pack_start(self.topbar, False, False, 0)

        # middle: video + sliding panel
        middle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        # stage: the black space available for video. The video widget (self.area, whose X
        # window the sink draws into) sits centred on top of it, sized to the scaled preview:
        # rkximagesink draws at the window's corner at the frame's own size and never
        # scales, so the widget must be exactly as big as the preview.
        overlay = Gtk.Overlay()
        self.stage = Gtk.DrawingArea()
        self.area = Gtk.DrawingArea()
        for w in (self.stage, self.area):
            w.connect("draw", self._on_draw)
            w.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.TOUCH_MASK)
            w.connect("button-press-event", self._on_video_tap)
        self.stage.connect("size-allocate", self._on_stage_resize)
        self.area.connect("size-allocate", self._on_video_resize)
        self.area.set_halign(Gtk.Align.CENTER)
        self.area.set_valign(Gtk.Align.CENTER)
        self.area.set_size_request(16, 16)
        overlay.add(self.stage)
        overlay.add_overlay(self.area)
        self.status = label("Starting…", "status", xalign=0.5)
        self.status.set_justify(Gtk.Justification.CENTER)
        self.status.set_halign(Gtk.Align.CENTER)
        self.status.set_valign(Gtk.Align.CENTER)
        self.status.set_line_wrap(True)
        self.status.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.status.set_max_width_chars(46)
        overlay.add_overlay(self.status)
        overlay.set_overlay_pass_through(self.status, True)
        middle.pack_start(overlay, True, True, 0)

        self.revealer = Gtk.Revealer()
        # no slide animation: every intermediate width would rescale the live preview
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.panels = Gtk.Stack()
        self.panels.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.panels.add_named(self._panel("Recording mode", self._build_mode_panel()), "mode")
        self.panels.add_named(self._panel("Background jobs", self._build_jobs_panel()), "jobs")
        self.panels.add_named(self._panel("Setup", self._build_setup_panel()), "setup")
        self.panels.add_named(self._panel("Gallery", self._build_gallery_panel()), "gallery")
        add_class(self.panels, "panel")
        self.revealer.add(self.panels)
        middle.pack_start(self.revealer, False, False, 0)
        main.pack_start(middle, True, True, 0)

        # bottom bar
        self.bottombar = add_class(Gtk.Box(spacing=16), "bar")
        self.stats_label = label("", "stats")
        self.stats_label.set_use_markup(True)
        self.toast_label = label("", "toast", xalign=1.0)
        self.toast_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.bottombar.pack_start(self.stats_label, False, False, 0)
        self.bottombar.pack_end(self.toast_label, True, True, 0)
        main.pack_start(self.bottombar, False, False, 0)
        main.pack_start(self._build_playbar(), False, False, 0)

        # sidebar
        self.sidebar = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12),
                                 "sidebar")
        self.sidebar.set_size_request(168, -1)
        self.mode_btn = self._side_button("MODE", "mode")
        self.rec_btn = RecordButton()
        self.rec_btn.connect("clicked", lambda *_: self.toggle_record())
        self.jobs_btn = self._side_button("JOBS", "jobs")
        self.setup_btn = self._side_button("SETUP", "setup")
        self.gallery_btn = self._side_button("GALLERY", "gallery")
        self.exit_btn = add_class(Gtk.Button(label="✕  EXIT"), "exit-btn", "danger")
        self.exit_btn.connect("clicked", lambda *_: self.quit(confirm=True))
        self.sidebar.pack_start(self.exit_btn, False, False, 0)
        self.sidebar.pack_start(self.mode_btn, False, False, 0)
        self.sidebar.pack_start(Gtk.Box(), True, True, 0)
        self.sidebar.pack_start(self.rec_btn, False, False, 0)
        self.rec_hint = label("", "note", xalign=0.5)
        self.rec_hint.set_justify(Gtk.Justification.CENTER)
        self.sidebar.pack_start(self.rec_hint, False, False, 0)
        self.sidebar.pack_start(Gtk.Box(), True, True, 0)
        self.sidebar.pack_start(self.gallery_btn, False, False, 0)
        self.sidebar.pack_start(self.jobs_btn, False, False, 0)
        self.sidebar.pack_start(self.setup_btn, False, False, 0)
        outer.pack_end(self.sidebar, False, False, 0)

        self.win.add(outer)
        self.win.show_all()
        self.revealer.set_reveal_child(False)
        self.panels.hide()
        self._refresh_mode_widgets()
        self.xid = self.area.get_window().get_xid()
        self.is_fullscreen = False
        if not self.args.windowed:
            self.toggle_fullscreen()
        GLib.timeout_add(250, self._tick)

    def _side_button(self, text, panel):
        b = add_class(Gtk.ToggleButton(), "side-btn")
        lb = Gtk.Label(label=text, justify=Gtk.Justification.CENTER)
        b.add(lb)
        b.text_label = lb
        b.connect("toggled", self._on_side_toggled, panel)
        return b

    def _panel(self, title, body):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=8)
        head.set_margin_start(22)
        head.set_margin_end(12)
        head.set_margin_top(12)
        head.pack_start(label(title, "panel-title"), True, True, 0)
        close = Gtk.Button(label="✕")
        close.connect("clicked", lambda *_: self.close_panel())
        head.pack_end(close, False, False, 0)
        box.pack_start(head, False, False, 0)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_kinetic_scrolling(True)
        sc.set_capture_button_press(True)
        add_class(body, "panel-body")
        sc.add(body)
        box.pack_start(sc, True, True, 0)
        box.set_size_request(min(640, int(self._screen_width() * 0.42)), -1)
        return box

    def _screen_width(self):
        scr = Gdk.Screen.get_default()
        return scr.get_width() if scr else 1920

    # ------------------------------------------------------------ mode panel

    def _build_mode_panel(self):
        s = self.s
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(label("CHOOSE HOW TO RECORD", "section"), False, False, 0)
        self.card_h265 = Card("H.265 · real time", "Hardware (VPU) encoder, small files, "
                              "ready to play and edit.")
        self.card_raw = Card("RAW · .arh", "Uncompressed frames straight from HDMI. "
                             "Optional HQ H.265 / FFV1 copies.")
        self.card_h265.connect("toggled", self._on_card, "h265")
        self.card_raw.connect("toggled", self._on_card, "raw")
        if not self.has_vpu:
            self.card_h265.set_sensitive(False)
            self.card_h265.sub.set_text("VPU encoder (mpph265enc) not available.")
        box.pack_start(self.card_h265, False, False, 0)
        box.pack_start(self.card_raw, False, False, 0)

        # --- H.265 options
        h = s["h265"]
        self.h265_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.h265_box.pack_start(label("BITRATE (Mb/s)", "section"), False, False, 0)
        self.seg_bitrate = Segmented([(b, str(b)) for b in H265_BITRATES], h["bitrate"],
                                     lambda v: self._set("h265", "bitrate", v))
        self.h265_box.pack_start(self.seg_bitrate, False, False, 0)
        self.h265_box.pack_start(label("RATE CONTROL", "section"), False, False, 0)
        self.h265_box.pack_start(Segmented(
            [("cbr", "Constant (CBR)"), ("vbr", "Variable (VBR)")], h["rc"],
            lambda v: self._set("h265", "rc", v)), False, False, 0)
        self.h265_box.pack_start(label("KEYFRAME EVERY", "section"), False, False, 0)
        self.h265_box.pack_start(Segmented(
            [(0.5, "0.5 s"), (1.0, "1 s"), (2.0, "2 s")], h["gop"],
            lambda v: self._set("h265", "gop", v)), False, False, 0)
        self.h265_box.pack_start(label("FILE TYPE", "section"), False, False, 0)
        self.h265_box.pack_start(Segmented(
            [("mp4", "MP4"), ("mkv", "MKV")], h["container"],
            lambda v: self._set("h265", "container", v)), False, False, 0)
        self.h265_note = label("", "note", wrap=True)
        self.h265_note.set_margin_top(14)
        self.h265_box.pack_start(self.h265_note, False, False, 0)
        box.pack_start(self.h265_box, False, False, 0)

        # --- RAW options
        r = s["raw"]
        self.raw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.raw_note = label("", "note", wrap=True)
        self.raw_note.set_margin_top(10)
        self.raw_box.pack_start(self.raw_note, False, False, 0)

        self.raw_box.pack_start(label("EXTRA COPIES", "section"), False, False, 0)
        self.hq_row = switch_row("High-quality H.265", r["hq"],
                                 lambda v: self._set("raw", "hq", v),
                                 "Encoded from the raw file into a .mov with PCM audio.")
        self.raw_box.pack_start(self.hq_row, False, False, 0)
        self.hq_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.hq_box.set_margin_start(12)
        self.hq_box.pack_start(label("ENCODER", "section"), False, False, 0)
        self.seg_engine = Segmented([("vpu", "VPU · fast"), ("x265", "x265 · best, slow")],
                                    r["hq_engine"], lambda v: self._set("raw", "hq_engine", v))
        self.seg_engine.set_option_sensitive("vpu", self.has_vpu)
        self.seg_engine.set_option_sensitive("x265", self.has_x265)
        self.hq_box.pack_start(self.seg_engine, False, False, 0)
        self.hq_box.pack_start(label("QUALITY", "section"), False, False, 0)
        self.hq_box.pack_start(Segmented(QUALITIES, r["hq_quality"],
                                         lambda v: self._set("raw", "hq_quality", v)),
                               False, False, 0)
        self.x265_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.x265_box.pack_start(label("x265 SPEED", "section"), False, False, 0)
        preset = r["x265_preset"] if r["x265_preset"] in dict(X265_SPEEDS) else "fast"
        self.x265_box.pack_start(Segmented(X265_SPEEDS, preset,
                                           lambda v: self._set("raw", "x265_preset", v)),
                                 False, False, 0)
        self.x265_box.pack_start(label("COLOUR", "section"), False, False, 0)
        self.x265_box.pack_start(Segmented(
            [("420", "4:2:0 · plays everywhere"), ("source", "Keep source chroma")],
            r["hq_chroma"], lambda v: self._set("raw", "hq_chroma", v)), False, False, 0)
        self.hq_box.pack_start(self.x265_box, False, False, 0)
        self.hq_speed_note = label("", "note warn", wrap=True)
        self.hq_box.pack_start(self.hq_speed_note, False, False, 0)
        self.raw_box.pack_start(self.hq_box, False, False, 0)

        self.ffv1_row = switch_row(
            "Lossless FFV1", r["ffv1"] and self.has_ffv1, lambda v: self._set("raw", "ffv1", v),
            "Mathematically lossless .mkv, about half the raw size. Encodes at roughly "
            "8–20 fps at 4K." if self.has_ffv1 else
            "Needs the FFV1 encoder:  sudo apt install gstreamer1.0-libav")
        self.ffv1_row.switch.set_sensitive(self.has_ffv1)
        self.ffv1_row.set_margin_top(10)
        self.raw_box.pack_start(self.ffv1_row, False, False, 0)
        self.ffv1_engine_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.ffv1_engine_box.set_margin_start(12)
        self.ffv1_engine_box.pack_start(label("FFV1 ENCODER", "section"), False, False, 0)
        self.ffv1_engine_box.pack_start(Segmented(
            [("cpu", "CPU · faster"), ("gpu", "GPU · frees the CPU")],
            r.get("ffv1_engine", "cpu"), lambda v: self._set("raw", "ffv1_engine", v)),
            False, False, 0)
        self.ffv1_engine_box.pack_start(label(
            "Same bit-exact file. At 4K: CPU ~14 fps using all cores; GPU ~5 fps using almost "
            "no CPU (and less power) - best while recording.", "note", wrap=True), False, False, 0)
        self.raw_box.pack_start(self.ffv1_engine_box, False, False, 0)

        self.when_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.when_box.pack_start(label("START ENCODING", "section"), False, False, 0)
        self.when_box.pack_start(Segmented(
            [("during", "While recording"), ("after", "After recording")], r["when"],
            lambda v: self._set("raw", "when", v)), False, False, 0)
        self.when_box.pack_start(label(
            "While recording: encodes follow the raw file as it is written and finish "
            "after you stop. After recording: nothing competes with the capture.",
            "note", wrap=True), False, False, 0)
        self.raw_box.pack_start(self.when_box, False, False, 0)
        box.pack_start(self.raw_box, False, False, 0)
        return box

    def _on_card(self, card, mode):
        if getattr(self, "_card_guard", False):
            return
        if self.capture and self.capture.recording:
            self.toast("Stop recording to change the mode")
            mode = self.s["mode"]
        self.s["mode"] = mode
        settings_mod.save(self.s)
        self._refresh_mode_widgets()

    def _set(self, group, key, value):
        self.s[group][key] = value
        settings_mod.save(self.s)
        self._refresh_mode_widgets()

    def _refresh_mode_widgets(self):
        s, r = self.s, self.s["raw"]
        self._card_guard = True
        self.card_h265.set_active(s["mode"] == "h265")
        self.card_raw.set_active(s["mode"] == "raw")
        self._card_guard = False
        self.h265_box.set_visible(s["mode"] == "h265")
        self.raw_box.set_visible(s["mode"] == "raw")
        self.hq_box.set_visible(r["hq"])
        self.x265_box.set_visible(r["hq_engine"] == "x265")
        self.when_box.set_visible(r["hq"] or (r["ffv1"] and self.has_ffv1))
        self.ffv1_engine_box.set_visible(r["ffv1"] and self.has_gpu_ffv1)
        warn_ctx = self.hq_speed_note.get_style_context()
        if r["hq_engine"] == "x265":
            warn_ctx.add_class("warn")
            self.hq_speed_note.set_text(
                "x265 runs on the CPU: at 4K expect about 0.2–1 fps, i.e. hours per minute "
                "of footage. It runs in the background; you can keep recording.")
        else:
            warn_ctx.remove_class("warn")
            self.hq_speed_note.set_text("VPU encodes at about real time (4K60), at a much "
                                        "higher quality than the live H.265 mode.")
        rate = self._rate(s["mode"])
        h = s["h265"]
        self.h265_note.set_text(f"About {fmt_bytes(rate * 3600)} per hour. "
                                f"{h['bitrate']} Mb/s suits 4K60; 40 Mb/s is plenty for 1080p.")
        t = self.capture.timing if getattr(self, "capture", None) else None
        fmt = self.capture.format if t else None
        what = f"{t[0]}×{t[1]}p{t[2]:.0f} {fmt}" if t else "4K60 NV12"
        self.raw_note.set_text(f"{what}: {fmt_bytes(rate)}/s, {fmt_bytes(rate * 60)} per "
                               f"minute. Needs a fast SSD.")
        # sidebar mode summary
        if s["mode"] == "h265":
            txt = f"MODE\nH.265\n{h['bitrate']} Mb/s"
        else:
            extra = []
            if r["hq"]:
                extra.append("+HQ")
            if r["ffv1"] and self.has_ffv1:
                extra.append("+FFV1")
            txt = "MODE\nRAW\n" + (" ".join(extra) if extra else ".arh")
        self.mode_btn.text_label.set_text(txt)

    def _rate(self, mode):
        """Bytes per second the current mode writes."""
        if mode == "h265":
            return self.s["h265"]["bitrate"] * 1e6 / 8 + 32000
        cap = getattr(self, "capture", None)
        w, h, fps = (cap.timing if cap and cap.timing else (3840, 2160, 60))
        bpp = FORMAT_BPP.get(cap.format if cap and cap.timing else "NV12", 3)
        return w * h * bpp * fps + 192000

    # ------------------------------------------------------------ jobs panel

    def _build_jobs_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.jobs_empty = label("No background encodes.\nThey appear here when a RAW "
                                "recording has HQ H.265 or FFV1 copies enabled.", "note",
                                wrap=True)
        self.jobs_empty.set_margin_top(16)
        box.pack_start(self.jobs_empty, False, False, 0)
        self.jobs_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(self.jobs_list, False, False, 0)
        clear = Gtk.Button(label="Clear finished")
        clear.set_margin_top(18)
        clear.connect("clicked", lambda *_: (self.jobs.clear_finished(), self._render_jobs()))
        box.pack_start(clear, False, False, 0)
        self.job_rows = {}
        return box

    def _job_row(self, job):
        row = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6), "job")
        top = Gtk.Box(spacing=10)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.pack_start(label(job.title, "job-title"), False, False, 0)
        name = label(os.path.basename(job.output), "note")
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        col.pack_start(name, False, False, 0)
        top.pack_start(col, True, True, 0)
        row.cancel = Gtk.Button(label="Cancel")
        row.cancel.connect("clicked", lambda *_: self.jobs.cancel(job))
        top.pack_end(row.cancel, False, False, 0)
        row.pack_start(top, False, False, 0)
        row.bar = Gtk.ProgressBar()
        row.pack_start(row.bar, False, False, 0)
        row.detail = label("", "stats")
        row.pack_start(row.detail, False, False, 0)
        return row

    def _render_jobs(self):
        for child in self.jobs_list.get_children():
            self.jobs_list.remove(child)
        self.job_rows = {}
        for job in reversed(self.jobs.jobs):
            row = self._job_row(job)
            self.job_rows[id(job)] = row
            self.jobs_list.pack_start(row, False, False, 0)
        self.jobs_list.show_all()
        for job in self.jobs.jobs:
            self._update_job_row(job, self.job_rows[id(job)])
        self.jobs_empty.set_visible(not self.jobs.jobs)
        self._update_jobs_button()

    def _update_job_row(self, job, row):
        row.cancel.set_visible(not job.finished)
        frac = job.frames / job.total if job.total else 0.0
        if job.state == "done":
            frac = 1.0
        row.bar.set_fraction(min(1.0, frac))
        if job.state == "queued":
            txt = "Waiting…"
        elif job.state == "running":
            txt = f"{job.frames} / {job.total} frames · {job.fps:.1f} fps"
            if job.live:
                txt += " · following recording"
            else:
                eta = job.eta()
                if eta is not None:
                    txt += f" · {fmt_duration(eta)} left"
        elif job.state == "done":
            txt = "Done ✓"
        elif job.state == "cancelled":
            txt = "Cancelled"
        else:
            txt = f"Failed: {job.error or 'unknown error'}"
        row.detail.set_text(txt)

    def _on_job_update(self, job):
        row = self.job_rows.get(id(job))
        if row is None:
            self._render_jobs()
        else:
            self._update_job_row(job, row)
        if job.state != getattr(job, "_announced", None):
            job._announced = job.state
            if job.state == "done":
                self.toast(f"{job.title} finished: {os.path.basename(job.output)}")
            elif job.state == "failed":
                self.toast(f"{job.title} failed — see Jobs")
            if job.finished and self.panels.get_visible() and \
                    self.panels.get_visible_child_name() == "gallery":
                self._scan_gallery()              # new copy / re-enable its button
        self._update_jobs_button()

    def _update_jobs_button(self):
        n = self.jobs.active_count()
        self.jobs_btn.text_label.set_text(f"JOBS\n{n} running" if n else "JOBS")

    # ------------------------------------------------------------ setup panel

    def _build_setup_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(label("SAVE RECORDINGS TO", "section"), False, False, 0)
        self.storage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(self.storage_box, False, False, 0)
        rescan = Gtk.Button(label="Look for drives again")
        rescan.connect("clicked", lambda *_: self._render_storage())
        box.pack_start(rescan, False, False, 0)
        self._render_storage()

        box.pack_start(label("AUDIO", "section"), False, False, 0)
        box.pack_start(switch_row("Record HDMI audio", self.s["audio"]["record"],
                                  lambda v: self._set("audio", "record", v)), False, False, 0)
        box.pack_start(switch_row("Listen (monitor out)", self.s["audio"]["monitor"],
                                  self._set_monitor), False, False, 0)

        box.pack_start(label("HDMI INPUT — WHAT THE BOARD ADVERTISES", "section"),
                       False, False, 0)
        self.edid_now = label("", "big", wrap=True)
        box.pack_start(self.edid_now, False, False, 0)
        self.seg_edid = Segmented([("4k60", "4K60"), ("4k30", "4K30"), ("1080p", "1080p60")],
                                  self.s["edid"], self._set_edid)
        box.pack_start(self.seg_edid, False, False, 0)
        box.pack_start(label("The source re-reads this and picks its output format. Use "
                             "4K30 if a 4K60 camera only sends 10-bit.", "note", wrap=True),
                       False, False, 0)
        box.pack_start(label("If the camera doesn't detect the board: plug the screen that "
                             "works with the camera into the board's HDMI OUT (or DP), then "
                             "copy its EDID here — the board then looks exactly like that "
                             "screen.", "note", wrap=True), False, False, 0)
        self.edid_copy_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(self.edid_copy_box, False, False, 0)
        row = Gtk.Box(spacing=10, homogeneous=True)
        rescan = Gtk.Button(label="Look for screens")
        rescan.connect("clicked", lambda *_: self._render_edid_sources())
        hpd = Gtk.Button(label="Re-send hot-plug")
        hpd.connect("clicked", lambda *_: self._set_edid(self.s["edid"]))
        row.pack_start(rescan, True, True, 0)
        row.pack_start(hpd, True, True, 0)
        box.pack_start(row, False, False, 0)
        self._render_edid_sources()
        if self.sim:
            box.pack_start(label("SIMULATED SOURCE", "section"), False, False, 0)
            b = Gtk.Button(label="Plug / unplug test signal")
            b.connect("clicked", lambda *_: self.sim.toggle())
            box.pack_start(b, False, False, 0)

        box.pack_start(label("TROUBLESHOOTING", "section"), False, False, 0)
        box.pack_start(label(f"Log: {short_path(log.LOG_PATH)}", "note", wrap=True),
                       False, False, 0)
        self.diag_btn = Gtk.Button(label="Create diagnostic report")
        self.diag_btn.connect("clicked", lambda *_: self.run_diagnostics())
        box.pack_start(self.diag_btn, False, False, 0)
        self.diag_label = label("Tests the HDMI input, display, encoder and audio (~20 s; "
                                "the live view pauses meanwhile).", "note", wrap=True)
        self.diag_label.set_selectable(True)
        box.pack_start(self.diag_label, False, False, 0)

        box.pack_start(label("APP", "section"), False, False, 0)
        row = Gtk.Box(spacing=10, homogeneous=True)
        fs = Gtk.Button(label="Full screen")
        fs.connect("clicked", lambda *_: self.toggle_fullscreen())
        row.pack_start(fs, True, True, 0)
        self.quit_btn = add_class(Gtk.Button(label="Quit"), "danger")
        self.quit_btn.connect("clicked", lambda *_: self.quit(confirm=True))
        row.pack_start(self.quit_btn, True, True, 0)
        box.pack_start(row, False, False, 0)
        return box

    def _storage_candidates(self):
        user = os.environ.get("USER", "")
        paths = [os.path.expanduser("~/Videos/HDMI-Recorder")]
        for root in (f"/media/{user}", "/media", "/mnt", f"/run/media/{user}"):
            try:
                for name in sorted(os.listdir(root)):
                    p = os.path.join(root, name)
                    if os.path.ismount(p) and os.access(p, os.W_OK):
                        paths.append(os.path.join(p, "HDMI-Recorder"))
            except OSError:
                pass
        if self.s["storage"] not in paths:
            paths.insert(0, self.s["storage"])
        return list(dict.fromkeys(paths))

    def _render_storage(self):
        for c in self.storage_box.get_children():
            self.storage_box.remove(c)
        self.storage_cards = []
        for p in self._storage_candidates():
            free = self._free_bytes(p)
            card = Card(short_path(p),
                        f"{fmt_bytes(free)} free" if free is not None else "not available")
            card.set_active(p == self.s["storage"])
            card.connect("toggled", self._on_storage_card, p)
            card.set_sensitive(free is not None)
            self.storage_box.pack_start(card, False, False, 0)
            self.storage_cards.append((card, p))
        self.storage_box.show_all()

    def _on_storage_card(self, card, path):
        if getattr(self, "_storage_guard", False):
            return
        self._storage_guard = True
        if self.capture and self.capture.recording:
            self.toast("Stop recording to change the storage location")
            path = self.s["storage"]
        for c, p in self.storage_cards:
            c.set_active(p == path)
        self._storage_guard = False
        self.s["storage"] = path
        settings_mod.save(self.s)

    def _set_monitor(self, on):
        self.s["audio"]["monitor"] = on
        settings_mod.save(self.s)
        if self.capture:
            self.capture.monitor_audio = on
            if self.capture.recording:
                self.toast("Monitoring changes after this recording")
            else:
                self.capture.restart()

    def _edid_label(self):
        e = self.s["edid"]
        if e.startswith("file:"):
            return f"Advertising: {self.s.get('edid_name') or os.path.basename(e[5:])}"
        return "Advertising: " + {"4k60": "4K60 test EDID", "4k30": "4K30 test EDID",
                                  "1080p": "1080p60 test EDID"}.get(e, e)

    def _render_edid_sources(self):
        for c in self.edid_copy_box.get_children():
            self.edid_copy_box.remove(c)
        for conn, name, data in v4l2.monitor_edids():
            b = Gtk.Button(label=f"Copy EDID of {name}  ({conn})")
            b.connect("clicked", self._copy_edid, conn, name, data)
            self.edid_copy_box.pack_start(b, False, False, 0)
        if not self.edid_copy_box.get_children():
            self.edid_copy_box.pack_start(label("No screen with a readable EDID is connected to "
                                                "the board.", "note", wrap=True), False, False, 0)
        self.edid_copy_box.show_all()
        self.edid_now.set_text(self._edid_label())
        self.seg_edid.set_value(self.s["edid"])

    def _copy_edid(self, _btn, conn, name, data):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{conn}-{name}")
        path = os.path.join(os.path.dirname(settings_mod.PATH), f"edid-{safe}.hex")
        v4l2.save_edid_hex(data, path)
        print(f"[info] copied EDID of {name} ({conn}, {len(data)} bytes) to {path}")
        self.s["edid_name"] = f"{name} (copied from {conn})"
        self._set_edid("file:" + path)

    def _set_edid(self, edid):
        self.s["edid"] = edid
        settings_mod.save(self.s)
        self.edid_now.set_text(self._edid_label())
        self.seg_edid.set_value(edid)
        if self.sim or not self.dev:
            return
        if self.capture and self.capture.recording:
            self.toast("Stop recording to change the HDMI input format")
            return
        self._apply_edid()
        self.toast("EDID updated — the source will re-detect the board")

    @staticmethod
    def _free_bytes(path):
        p = path
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        try:
            return shutil.disk_usage(p or "/").free
        except OSError:
            return None

    # ------------------------------------------------------------ panels / chrome

    def _on_side_toggled(self, button, panel):
        if getattr(self, "_side_guard", False):
            return
        self._side_guard = True
        for b, name in ((self.mode_btn, "mode"), (self.jobs_btn, "jobs"),
                        (self.setup_btn, "setup"), (self.gallery_btn, "gallery")):
            if name != panel:
                b.set_active(False)
        self._side_guard = False
        if button.get_active():
            if panel == "jobs":
                self._render_jobs()
            elif panel == "setup":
                self._render_storage()
            elif panel == "gallery":
                self._scan_gallery()
            else:
                self._refresh_mode_widgets()
            self.panels.set_visible_child_name(panel)
            self.panels.show()
            self.revealer.set_reveal_child(True)
        else:
            self.revealer.set_reveal_child(False)
            self.panels.hide()

    def close_panel(self):
        self._side_guard = True
        for b in (self.mode_btn, self.jobs_btn, self.setup_btn, self.gallery_btn):
            b.set_active(False)
        self._side_guard = False
        self.revealer.set_reveal_child(False)
        self.panels.hide()

    def _on_video_tap(self, _w, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if self.revealer.get_reveal_child():
            self.close_panel()
            return True
        self.chrome_visible = not self.chrome_visible
        for w in (self.topbar, self.bottombar, self.sidebar):
            w.set_visible(self.chrome_visible)
        return True

    def _on_stage_resize(self, _w, alloc):
        box = (alloc.width, alloc.height)
        old = getattr(self, "_stage_box", None)
        if box != old:
            self._stage_box = box
            if getattr(self, "_layout_timer", None):
                GLib.source_remove(self._layout_timer)
                self._layout_timer = None
            if old and (box[0] < old[0] or box[1] < old[1]):
                GLib.idle_add(self._layout_video)     # shrinking: at once, never overflow
            else:
                # growing: once the size has settled (fullscreen etc. come in steps)
                self._layout_timer = GLib.timeout_add(120, self._layout_settled)

    def _layout_settled(self):
        self._layout_timer = None
        self._layout_video()
        return False

    def _layout_video(self):
        """Fit the preview into the stage and size the video widget to match."""
        cap = getattr(self, "capture", None)
        box = getattr(self, "_stage_box", None)
        if getattr(self, "player", None) and box:
            self._relayout_player(box)
            return False
        if not cap or not box:
            return False
        size = cap.set_preview_box(*box)
        if cap.pipeline and cap.timing and size:
            self.area.set_size_request(*size)
        else:
            self.area.set_size_request(16, 16)
        return False

    def _on_video_resize(self, *_):
        if getattr(self, "capture", None):
            GLib.idle_add(self.capture.expose)

    def _on_draw(self, _w, cr):
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        return False

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.win.unfullscreen()
        else:
            self.win.fullscreen()
        self.is_fullscreen = not self.is_fullscreen

    def _on_key(self, _w, event):
        key = Gdk.keyval_name(event.keyval).lower()
        if key == "space" and getattr(self, "player", None):
            self.player.toggle()
        elif key in ("space", "r", "return"):
            self.toggle_record()
        elif key in ("f", "f11"):
            self.toggle_fullscreen()
        elif key == "escape" and self.player:
            self.exit_playback()
        elif key == "escape":
            if self.revealer.get_reveal_child():
                self.close_panel()
            elif self.is_fullscreen:
                self.toggle_fullscreen()
        elif key == "q":
            self.quit(confirm=True)
        elif key == "s" and self.sim:
            self.sim.toggle()
        return True

    def toast(self, text):
        print(f"[ui] {text}")
        self.toast_label.set_text(text)
        self.last_toast = time.monotonic()

    # ------------------------------------------------------------ capture listener

    def on_sync_message(self, _bus, msg):
        # streaming thread: embed the video sink in our drawing area
        if GstVideo.is_video_overlay_prepare_window_handle_message(msg):
            msg.src.set_window_handle(self.xid)

    def on_signal(self, text):
        GLib.idle_add(self._layout_video)
        if text and self.capture and self.capture.preview_disabled:
            self.sig_label.set_text(f"● {text}")
            self.status.set_text("Live preview unavailable (no display found).\n"
                                 "Capture and recording still work.\nRestart the app after "
                                 "connecting a screen.")
            self.status.set_visible(True)
            return
        if text:
            self.sig_label.set_text(f"● {text}")
            self.sig_label.get_style_context().remove_class("signal-off")
            self.sig_label.get_style_context().add_class("signal-on")
            self.status.set_visible(False)
        else:
            self.sig_label.set_text("● No signal")
            self.sig_label.get_style_context().remove_class("signal-on")
            self.sig_label.get_style_context().add_class("signal-off")
            if self.dev and not self.sim:
                state, why = v4l2.link_state(self.dev)
                short = {"none": "nothing connected", "idle": "source not sending",
                         "unlocked": "can't lock", "unsupported": "unsupported format"}
                self.sig_label.set_text(f"● No signal · {short.get(state, state)}")
                if state != getattr(self, "_last_link_state", None):
                    self._last_link_state = state
                    print(f"[info] HDMI link: {state} — {why}")
                self.status.set_text(f"No HDMI signal\n{why}\n\n{self._edid_label()}  "
                                     "(SETUP → HDMI INPUT)")
            else:
                self.status.set_text("No HDMI signal")
            self.status.set_visible(True)
        self.area.queue_draw()
        self._refresh_mode_widgets()

    def on_capture_error(self, message):
        self.toast(f"Video error: {message}")
        self.status.set_text(f"Video error\n{message}\n\nRetrying automatically. Details: "
                             f"{short_path(log.LOG_PATH)}\nSETUP → Create diagnostic report")
        self.status.set_visible(True)
        self.area.queue_draw()

    def on_recording_stopped(self, rec, reason):
        self._recording_finished(rec, reason)

    # ------------------------------------------------------------ recording

    def toggle_record(self):
        if not self.capture:
            return
        if getattr(self, "player", None):
            self.toast("Close playback to record")
            return
        rec = self.capture.recording
        if rec:
            if not rec.stopping:
                self.rec_btn.set_state("busy")
                rec.stop(on_done=lambda r: self._recording_finished(r, None))
            return
        self.start_recording()

    def start_recording(self):
        s = self.s
        if not self.capture.timing:
            self.toast("No HDMI signal to record")
            return
        mode = s["mode"]
        if mode == "h265" and not self.has_vpu:
            self.toast("VPU encoder not available — use RAW")
            return
        folder = s["storage"]
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.toast(f"Cannot use {folder}: {e.strerror}")
            return
        free = self._free_bytes(folder) or 0
        if free < 3e9:
            self.toast(f"Not enough space on {folder} ({fmt_bytes(free)} free)")
            return
        base = os.path.join(folder, datetime.now().strftime("REC_%Y%m%d_%H%M%S"))
        opts = s["h265"] if mode == "h265" else s["raw"]
        try:
            rec = self.capture.start_recording(mode, base, opts, s["audio"]["record"])
        except (RuntimeError, GLib.Error) as e:
            self.toast(f"Could not start recording: {e}")
            return
        rec.extra_jobs = self._planned_jobs() if mode == "raw" else []
        if mode == "raw" and s["raw"]["when"] == "during":
            for codec, o in rec.extra_jobs:
                self.jobs.add(rec.path, codec, o, follow=True)
            rec.extra_jobs = []
        self.rec_btn.set_state("recording")
        self.rec_hint.set_text("REC")
        if s["audio"]["record"] and not rec.with_audio:
            self.toast("Recording without audio (no HDMI audio detected)")
        else:
            self.toast(f"Recording {os.path.basename(rec.path)}")

    def _planned_jobs(self):
        r = self.s["raw"]
        out = []
        if r["hq"]:
            codec = "h265-x265" if r["hq_engine"] == "x265" or not self.has_vpu else "h265-vpu"
            out.append((codec, {"quality": r["hq_quality"], "preset": r["x265_preset"],
                                "chroma": r["hq_chroma"]}))
        if r["ffv1"] and r.get("ffv1_engine") == "gpu" and self.has_gpu_ffv1:
            out.append(("ffv1-gpu", {}))
        elif r["ffv1"] and self.has_ffv1:
            out.append(("ffv1", {}))
        return out

    def _recording_finished(self, rec, reason):
        if id(rec) in self.rec_done:
            return
        self.rec_done.add(id(rec))
        self.rec_btn.set_state("idle" if self.capture and self.capture.timing else "disabled")
        self.rec_hint.set_text("")
        size = rec.size_bytes()
        msg = f"Saved {os.path.basename(rec.path)} · {fmt_bytes(size)} · " \
              f"{fmt_tc(getattr(rec, 'duration', rec.elapsed()))}"
        if reason:
            msg = f"Recording stopped ({reason}). " + msg
        if rec.error:
            msg += f" · {rec.error}"
        self.toast(msg)
        if size > 0 and os.path.exists(rec.path):
            for codec, o in getattr(rec, "extra_jobs", []):
                self.jobs.add(rec.path, codec, o, follow=False)
        self._render_jobs()

    # ------------------------------------------------------------ timers

    def _poll(self):
        if self.capture:
            self.capture.poll()
        return True

    def _tick(self):
        if getattr(self, "player", None):
            self._tick_player()
            return True
        cap = getattr(self, "capture", None)
        rec = cap.recording if cap else None
        ctx = self.tc_label.get_style_context()
        if rec and not rec.stopping:
            el = rec.elapsed()
            self.tc_label.set_text(("● " if int(el * 2) % 2 == 0 else "   ") + fmt_tc(el))
            ctx.add_class("tc-rec")
            st = rec.stats()
            parts = [f"{rec.kind}", fmt_bytes(rec.size_bytes())]
            if el > 1:
                parts.append(f"{rec.size_bytes() * 8 / el / 1e6:.0f} Mb/s")
            if rec.with_audio:
                parts.append("audio ✓")
            if "buffer" in st:
                parts.append(f"buffer {st['buffer'] * 100:.0f}%")
            drops = st.get("drops", 0) + st.get("disk_drops", 0)
            text = " · ".join(GLib.markup_escape_text(p) for p in parts)
            if drops:
                text += f"  <span foreground='#ff453a'><b>{drops} frames dropped</b></span>"
            self.stats_label.set_markup(text)
            self._guard_resources(rec)
        else:
            self.tc_label.set_text(fmt_tc(0))
            ctx.remove_class("tc-rec")
            n = self.jobs.active_count()
            self.stats_label.set_markup(GLib.markup_escape_text(
                f"{n} background encode{'s' if n != 1 else ''} running" if n else "Ready"))
            if cap and not rec:
                self.rec_btn.set_state("idle" if cap.timing else "disabled")
        if cap and cap.timing and cap.pipeline:
            nominal = cap.timing[2]
            m = cap.fps_measured
            txt = f"● {cap.signal_text()}"
            if m and nominal - m > max(3.0, nominal * 0.08):
                txt += f" · only {m:.0f} fps arriving"
            self.sig_label.set_text(txt)
        # storage
        free = self._free_bytes(self.s["storage"])
        if free is not None:
            rate = self._rate(self.s["mode"])
            self.store_label.set_text(f"{fmt_bytes(free)} free · ≈ {fmt_duration(free / rate)}")
        if self.last_toast and time.monotonic() - self.last_toast > 12:
            self.toast_label.set_text("")
            self.last_toast = 0.0
        if self.quit_armed and time.monotonic() - self.quit_armed > 4:
            self.quit_armed = 0.0
            self.quit_btn.set_label("Quit")
            self.exit_btn.set_label("✕  EXIT")
        return True

    def _guard_resources(self, rec):
        free = self._free_bytes(self.s["storage"]) or 0
        if free < 1.5e9:
            rec.stop(on_done=lambda r: self._recording_finished(r, "disk almost full"))
            self.rec_btn.set_state("busy")
            return
        try:
            with open("/proc/meminfo") as f:
                avail = next(int(l.split()[1]) * 1024 for l in f if l.startswith("MemAvailable"))
        except (OSError, StopIteration):
            return
        if avail < 600e6:
            rec.stop(on_done=lambda r: self._recording_finished(r, "system memory exhausted"))
            self.rec_btn.set_state("busy")

    # ------------------------------------------------------------ diagnostics

    def _repair_interrupted(self):
        """Finalize .arh files left open by a crash or power loss."""
        try:
            names = [n for n in os.listdir(self.s["storage"]) if n.endswith(".arh")]
        except OSError:
            return False
        for n in names:
            path = os.path.join(self.s["storage"], n)
            try:
                frames = arh.repair(path)
            except (OSError, ValueError) as e:
                print(f"[warn] could not check {path}: {e}")
                continue
            if frames is not None:
                self.toast(f"Recovered interrupted recording {n} ({frames} frames)")
        return False

    def run_diagnostics(self):
        if not self.capture:
            return
        if self.capture.recording:
            self.toast("Stop recording before running diagnostics")
            return
        self.diag_btn.set_sensitive(False)
        self.diag_btn.set_label("Running diagnostics…")
        self.capture.paused = True
        self.capture.stop("diagnostics")
        self.status.set_text("Running diagnostics…")
        self.status.set_visible(True)
        cmd = [sys.executable, self.main_script, "diag"]
        if self.dev:
            cmd += ["-d", self.dev]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
        except OSError as e:
            self._diag_done(None, str(e))
            return
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, proc.pid,
                             lambda _pid, _st: self._diag_done(proc, None))

    def _diag_done(self, proc, error):
        out = proc.stdout.read() if proc else ""
        if proc:
            proc.wait()
        m = re.search(r"Report written to (\S+)", out)
        if m:
            self.diag_label.set_text(f"Report: {short_path(m.group(1))}")
            self.toast(f"Diagnostic report saved: {short_path(m.group(1))}")
            print(f"[info] diagnostic report: {m.group(1)}")
        else:
            self.diag_label.set_text(f"Diagnostics failed: {error or out[-300:]}")
        self.diag_btn.set_sensitive(True)
        self.diag_btn.set_label("Create diagnostic report")
        self.capture.paused = False
        self.capture.candidate, self.capture.candidate_count = None, 0
        self.capture.retry_at = 0.0

    # ------------------------------------------------------------ gallery

    def _build_gallery_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.gallery_filter = "all"
        self.seg_gallery = Segmented([("all", "All"), (library.RAW, "RAW"),
                                      (library.H265, "H.265"), (library.FFV1, "FFV1")],
                                     "all", self._set_gallery_filter)
        box.pack_start(self.seg_gallery, False, False, 0)
        self.gallery_note = label("", "note", wrap=True)
        box.pack_start(self.gallery_note, False, False, 0)
        self.gallery_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.pack_start(self.gallery_list, False, False, 0)
        refresh = Gtk.Button(label="Refresh")
        refresh.set_margin_top(14)
        refresh.connect("clicked", lambda *_: self._scan_gallery())
        box.pack_start(refresh, False, False, 0)
        self.library = library.Library()
        self.gallery_clips = []
        return box

    def _set_gallery_filter(self, kind):
        self.gallery_filter = kind
        self._render_gallery()

    def _scan_gallery(self):
        self.gallery_note.set_text(f"Looking in {short_path(self.s['storage'])}…")
        self.library.scan(self.s["storage"],
                          lambda clips: GLib.idle_add(self._gallery_scanned, clips))

    def _gallery_scanned(self, clips):
        self.gallery_clips = clips
        self._render_gallery()
        return False

    def _render_gallery(self):
        for c in self.gallery_list.get_children():
            self.gallery_list.remove(c)
        f = self.gallery_filter
        shown = 0
        for clip in self.gallery_clips:
            items = [i for i in clip.items if f == "all" or i.kind == f]
            if not items:
                continue
            shown += 1
            card = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6), "job")
            card.pack_start(label(clip.title, "clip-title"), False, False, 0)
            for item in items:
                card.pack_start(self._gallery_item(item), False, False, 0)
                if item.kind == library.RAW:
                    card.pack_start(self._encode_row(item), False, False, 0)
            self.gallery_list.pack_start(card, False, False, 0)
        total = len(self.gallery_clips)
        what = "recordings" if f == "all" else f"recordings with a {f} file"
        self.gallery_note.set_text(
            f"{shown} {what} in {short_path(self.s['storage'])}" if total else
            f"No recordings in {short_path(self.s['storage'])} yet.")
        self.gallery_list.show_all()

    def _encode_row(self, item):
        """Buttons that make H.265 / FFV1 copies of a RAW recording (background jobs)."""
        row = add_class(Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True), "enc")
        r = self.s["raw"]
        h265_opts = {"quality": r["hq_quality"], "preset": r["x265_preset"],
                     "chroma": r["hq_chroma"]}
        targets = [("h265-vpu", "H.265 · VPU", h265_opts, self.has_vpu),
                   ("h265-x265", "H.265 · x265", h265_opts, self.has_x265),
                   ("ffv1", "FFV1 · CPU", {}, self.has_ffv1)]
        if self.has_gpu_ffv1:
            targets.append(("ffv1-gpu", "FFV1 · GPU", {}, True))
        for n, (codec, text, opts, available) in enumerate(targets):
            out = jobs.output_path(item.path, codec)
            busy = any(j.output == out and not j.finished for j in self.jobs.jobs)
            if busy:
                label_text, sensitive = f"Encoding {text.split(' ·')[0]}…", False
            elif os.path.exists(out):
                label_text, sensitive = f"✓ {text.split(' ·')[0]} made", False
            else:
                label_text, sensitive = f"Encode {text}", available
            b = Gtk.Button(label=label_text)
            b.set_sensitive(sensitive)
            b.connect("clicked", self._encode_item, item, codec, opts, text)
            row.attach(b, n % 2, n // 2, 1, 1)
        return row

    def _encode_item(self, _btn, item, codec, opts, text):
        self.jobs.add(item.path, codec, dict(opts), follow=False)
        self.toast(f"Encoding {item.name} to {text} — progress in JOBS")
        self._render_gallery()

    def _gallery_item(self, item):
        b = add_class(Gtk.Button(), "gitem")
        row = Gtk.Box(spacing=12)
        badge = label(item.kind, "badge " + BADGE.get(item.kind, "badge-other"), xalign=0.5)
        badge.set_valign(Gtk.Align.CENTER)
        row.pack_start(badge, False, False, 0)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = label(item.name, "note")
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        col.pack_start(name, False, False, 0)
        col.pack_start(label(item.summary() + ("  · with audio" if item.audio else "")),
                       False, False, 0)
        if item.problem:
            col.pack_start(label(item.problem, "note warn", wrap=True), False, False, 0)
        row.pack_start(col, True, True, 0)
        row.pack_end(label("▶", "big"), False, False, 0)
        b.add(row)
        playable = not (item.problem and item.problem.startswith("can't"))
        b.set_sensitive(playable)
        b.connect("clicked", lambda *_: self.play_item(item))
        return b

    # ------------------------------------------------------------ playback

    def _build_playbar(self):
        self.playbar = add_class(Gtk.Box(spacing=10), "playbar")
        self.play_close = Gtk.Button(label="✕  Close")
        self.play_close.connect("clicked", lambda *_: self.exit_playback())
        back = Gtk.Button(label="⟲ 10 s")
        back.connect("clicked", lambda *_: self.player and
                     self.player.seek(self.player.position() - 10))
        self.play_btn = Gtk.Button(label="Pause")
        self.play_btn.connect("clicked", lambda *_: self.player and self.player.toggle())
        fwd = Gtk.Button(label="10 s ⟳")
        fwd.connect("clicked", lambda *_: self.player and
                    self.player.seek(self.player.position() + 10))
        self.play_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.1)
        self.play_scale.set_draw_value(False)
        self.play_scale.connect("change-value", self._on_scrub)
        self.play_time = label("0:00 / 0:00", "stats")
        for w in (self.play_close, back, self.play_btn, fwd):
            self.playbar.pack_start(w, False, False, 0)
        self.playbar.pack_start(self.play_scale, True, True, 0)
        self.playbar.pack_start(self.play_time, False, False, 0)
        for child in self.playbar.get_children():
            child.show_all()
        # hidden until playback; (the window's show_all must not show it)
        self.playbar.set_no_show_all(True)
        return self.playbar

    def _on_scrub(self, _scale, _scroll, value):
        if self.player:
            self.player.seek(value)
        return False

    def play_item(self, item):
        if self.capture and self.capture.recording:
            self.toast("Stop recording before playing a recording")
            return
        self.close_panel()
        self._stop_player()
        if self.capture:
            self.capture.paused = True        # the player needs the display plane
            self.capture.stop("playback")
        plane = self.capture.plane_id if self.capture else None
        try:
            self.player = player.Player(item, self._stage_box or (1280, 720), self, plane)
        except (OSError, ValueError, GLib.Error) as e:
            self.player = None
            self.toast(f"Can't play {item.name}: {e}")
            self.exit_playback()
            return
        print(f"[info] playback: {item.path} [{item.kind}] at {self.player.size}")
        self.area.set_size_request(*self.player.size)
        self.status.set_visible(False)
        self.bottombar.hide()
        self.playbar.show()
        self.rec_btn.set_state("disabled")
        self.sig_label.set_text(f"▶ {item.kind} · {item.name}")
        self.player.play()

    def _relayout_player(self, box):
        p = self.player
        if player.fit(p.item.width or 1920, p.item.height or 1080, box) == p.size:
            return
        pos, playing, item = p.position(), p.playing, p.item
        p.stop()
        self.player = player.Player(item, box, self, p.plane_id)
        self.area.set_size_request(*self.player.size)
        self.player.play()

        def resume():
            if self.player:
                self.player.seek(pos)
                if not playing:
                    self.player.pause()
            return False
        GLib.timeout_add(300, resume)

    def _stop_player(self):
        p = getattr(self, "player", None)
        self.player = None
        if p:
            p.stop()

    def exit_playback(self):
        self._stop_player()
        self.playbar.hide()
        self.bottombar.show()
        if self.capture:
            self.capture.paused = False
            self.capture.candidate, self.capture.candidate_count = None, 0
            self.capture.retry_at = 0.0
            self.capture.poll()
        self._layout_video()

    def on_player_event(self, p, event, detail):
        if p is not self.player:
            return
        if event == "playing":
            self.play_btn.set_label("Pause")
        elif event == "paused":
            self.play_btn.set_label("Play")
        elif event == "ended":
            self.play_btn.set_label("Replay")
        elif event == "error":
            self.toast(f"Playback error: {detail}")
            self.play_btn.set_label("Play")

    def _tick_player(self):
        p = self.player
        pos, dur = p.position(), p.duration()
        self.tc_label.set_text(fmt_tc(pos))
        self.tc_label.get_style_context().remove_class("tc-rec")
        if dur > 0:
            self.play_scale.get_adjustment().set_upper(dur)
            self.play_scale.set_value(min(pos, dur))
        self.play_time.set_text(f"{library._dur(pos)} / {library._dur(dur)}")

    # ------------------------------------------------------------ lifecycle

    def quit(self, confirm=False):
        busy = (self.capture and self.capture.recording) or self.jobs.active_count()
        if confirm and busy and not self.quit_armed:
            self.quit_armed = time.monotonic()
            what = "a recording" if self.capture and self.capture.recording else \
                "background encodes"
            self.quit_btn.set_label("Tap again to quit")
            self.exit_btn.set_label("TAP AGAIN\nTO EXIT")
            self.toast(f"Quitting stops {what}. Tap EXIT again to confirm.")
            return
        self._stop_player()
        if self.capture:
            self.capture.stop("quit")
        self.jobs.shutdown()
        self.loop.quit()

    def run(self):
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            if self.capture:
                self.capture.stop("quit")
            self.jobs.shutdown()


def run(args, main_script):
    App(args, main_script).run()
