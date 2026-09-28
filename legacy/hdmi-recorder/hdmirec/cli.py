"""Command-line recorder: the touch app without a screen (works over SSH).

    python3 hdmi_recorder.py cli                      # interactive
    python3 hdmi_recorder.py cli --mode raw --ffv1    # RAW + lossless copy
    python3 hdmi_recorder.py cli --record --duration 60 --mode h265 --bitrate 60

Keys: r / space  start-stop recording   m  switch mode (H.265 <-> RAW)
      q          quit (asks again if something is running)   h  help

Settings default to the touch app's (~/.config/hdmi-recorder/settings.json); flags
override them for this run only (add --save to keep them).
"""
import os
import shutil
import sys
import termios
import time
import tty
from datetime import datetime

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from . import gpu, jobs, library, log, settings as settings_mod, v4l2  # noqa: E402
from .capture import Capture  # noqa: E402

HELP = ("keys: [r]/[space] record/stop  [m] mode  [q] quit  [h] help")


def add_cli_args(sp):
    sp.add_argument("-d", "--device", help="V4L2 device (default: auto-detect hdmirx)")
    sp.add_argument("--mode", choices=["h265", "raw"], help="recording mode")
    sp.add_argument("--bitrate", type=int, help="H.265: Mbit/s (e.g. 40, 80, 120)")
    sp.add_argument("--rc", choices=["cbr", "vbr"], help="H.265: rate control")
    sp.add_argument("--gop", type=float, help="H.265: keyframe interval in seconds")
    sp.add_argument("--container", choices=["mp4", "mkv"], help="H.265: file type")
    sp.add_argument("--hq", choices=["off", "vpu", "x265"],
                    help="RAW: also make a high-quality H.265 copy with this encoder")
    sp.add_argument("--quality", choices=["high", "higher", "max"], help="RAW: HQ copy quality")
    sp.add_argument("--ffv1", action=argparse_bool(), help="RAW: also make a lossless FFV1 copy")
    sp.add_argument("--ffv1-engine", choices=["cpu", "gpu"],
                    help="RAW: encode the FFV1 copy on the CPU (faster) or GPU (frees the CPU)")
    sp.add_argument("--when", choices=["during", "after"], help="RAW: when the copies are encoded")
    sp.add_argument("--no-audio", action="store_true", help="don't record audio")
    sp.add_argument("--storage", help="folder for recordings")
    sp.add_argument("--edid", choices=["4k60", "4k30", "1080p", "keep"],
                    help="what the board advertises to the source")
    sp.add_argument("--record", action="store_true", help="start recording as soon as there is a signal")
    sp.add_argument("--duration", type=float, help="stop recording after this many seconds, then exit")
    sp.add_argument("--no-wait", action="store_true",
                    help="with --duration: exit without waiting for background encodes")
    sp.add_argument("--save", action="store_true", help="save the flags as the new defaults")
    sp.add_argument("--simulate", metavar="WxH@FPS[:FMT[:PATTERN]]", help="test pattern instead of HDMI RX")


def argparse_bool():
    import argparse
    return argparse.BooleanOptionalAction


def _apply_flags(s, a):
    h, r = s["h265"], s["raw"]
    for key, val in (("mode", a.mode), ("storage", a.storage), ("edid", a.edid)):
        if val is not None:
            s[key] = os.path.expanduser(val) if key == "storage" else val
    for key, val in (("bitrate", a.bitrate), ("rc", a.rc), ("gop", a.gop), ("container", a.container)):
        if val is not None:
            h[key] = val
    if a.hq is not None:
        r["hq"] = a.hq != "off"
        if a.hq != "off":
            r["hq_engine"] = a.hq
    for key, val in (("hq_quality", a.quality), ("ffv1", a.ffv1), ("when", a.when),
                     ("ffv1_engine", a.ffv1_engine)):
        if val is not None:
            r[key] = val
    if a.no_audio:
        s["audio"]["record"] = False
    s["audio"]["monitor"] = False            # no speakers involved on the command line


def _fmt_tc(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class StatusOut:
    """stdout for the interactive CLI: messages go to the log and to the terminal, above a
    status line that is redrawn in place (the status line itself is not logged)."""

    def __init__(self, real, term_fd):
        self.real = real            # the log pipe
        self.term = term_fd
        self.status = ""

    def _tty(self, text):
        try:
            os.write(self.term, text.encode("utf-8", "replace"))
        except OSError:
            pass

    def write(self, text):
        if not text:
            return 0
        self.real.write(text)
        self.real.flush()
        self._tty("\r\033[K" + text + (self.status if text.endswith("\n") else ""))
        return len(text)

    def set_status(self, line):
        width = shutil.get_terminal_size((120, 20)).columns - 1
        self.status = line[:width]
        self._tty("\r\033[K" + self.status)

    def flush(self):
        self.real.flush()

    def isatty(self):
        return self.real.isatty()

    def fileno(self):
        return self.real.fileno()


class Cli:
    def __init__(self, args, main_script):
        self.args = args
        self.s = settings_mod.load()
        _apply_flags(self.s, args)
        if args.save:
            settings_mod.save(self.s)
        self.loop = GLib.MainLoop()
        self.has_vpu = bool(Gst.ElementFactory.find("mpph265enc"))
        self.has_ffv1 = bool(Gst.ElementFactory.find("avenc_ffv1"))
        if self.s["mode"] == "h265" and not self.has_vpu:
            print("[warn] VPU encoder unavailable, using RAW mode")
            self.s["mode"] = "raw"
        self.sim = v4l2.SimulatedSignal(args.simulate) if args.simulate else None
        self.dev = None if self.sim else (args.device or v4l2.find_hdmirx_device())
        if not self.sim and not self.dev:
            sys.exit("HDMI RX device not found (enable it with setup_hdmirx.sh and reboot).")
        self.jobs = jobs.JobManager(main_script, self._on_job)
        self.quit_armed = 0.0
        self.recorded = 0
        self.deadline = None
        self.tty = sys.stdin.isatty()
        self._term = None
        self.last_link = None
        if not self.sim and self.s["edid"] in v4l2.EDID_TYPES:
            v4l2.set_edid(self.dev, self.s["edid"])
        self.cap = Capture(self.dev, self, sink="fakesink",
                           audio_card=None if self.sim else v4l2.find_hdmiin_audio(),
                           monitor_audio=False, simulate=self.sim)

    # ------------------------------------------------------------ capture listener

    def on_signal(self, text):
        if text is None and self.dev and not self.sim:
            state, why = v4l2.link_state(self.dev)
            if state != self.last_link:
                self.last_link = state
                print(f"[info] no signal: {why}")
        elif text:
            self.last_link = "ok"

    def on_capture_error(self, message):
        print(f"[error] {message}")

    def on_sync_message(self, *_):
        pass

    def on_recording_stopped(self, rec, reason):
        self._finished(rec, reason)

    # ------------------------------------------------------------ recording

    def toggle(self):
        rec = self.cap.recording
        if rec:
            if not rec.stopping:
                print("[info] stopping…")
                rec.stop(on_done=lambda r: self._finished(r, None))
            return
        self.start()

    def start(self):
        s = self.s
        if not self.cap.timing:
            print("[warn] no HDMI signal to record")
            return False
        folder = s["storage"]
        try:
            os.makedirs(folder, exist_ok=True)
            free = shutil.disk_usage(folder).free
        except OSError as e:
            print(f"[error] can't use {folder}: {e}")
            return False
        if free < 3e9:
            print(f"[error] not enough space on {folder} ({library._size(free)} free)")
            return False
        base = os.path.join(folder, datetime.now().strftime("REC_%Y%m%d_%H%M%S"))
        mode = s["mode"]
        try:
            rec = self.cap.start_recording(mode, base, s["h265"] if mode == "h265" else s["raw"],
                                           s["audio"]["record"])
        except (RuntimeError, GLib.Error) as e:
            print(f"[error] could not start recording: {e}")
            return False
        rec.extra_jobs = self._planned_jobs() if mode == "raw" else []
        if mode == "raw" and s["raw"]["when"] == "during":
            for codec, o in rec.extra_jobs:
                self.jobs.add(rec.path, codec, o, follow=True)
            rec.extra_jobs = []
        if self.args.duration:
            self.deadline = time.monotonic() + self.args.duration
        print(f"[info] ● recording {os.path.basename(rec.path)}"
              + ("" if rec.with_audio else " (no audio)"))
        return True

    def _planned_jobs(self):
        r = self.s["raw"]
        out = []
        if r["hq"]:
            codec = "h265-x265" if r["hq_engine"] == "x265" or not self.has_vpu else "h265-vpu"
            out.append((codec, {"quality": r["hq_quality"], "preset": r["x265_preset"],
                                "chroma": r["hq_chroma"]}))
        if r["ffv1"] and r.get("ffv1_engine") == "gpu" and gpu.available():
            out.append(("ffv1-gpu", {}))
        elif r["ffv1"] and self.has_ffv1:
            out.append(("ffv1", {}))
        return out

    def _finished(self, rec, reason):
        if getattr(rec, "_cli_done", False):
            return
        rec._cli_done = True
        self.recorded += 1
        self.deadline = None
        size = rec.size_bytes()
        print(f"[info] saved {rec.path} · {library._size(size)} · "
              f"{_fmt_tc(getattr(rec, 'duration', rec.elapsed()))}"
              + (f" (stopped: {reason})" if reason else "") + (f" · {rec.error}" if rec.error else ""))
        if size > 0 and os.path.exists(rec.path):
            for codec, o in getattr(rec, "extra_jobs", []):
                self.jobs.add(rec.path, codec, o, follow=False)

    def _on_job(self, job):
        if job.state in ("done", "failed", "cancelled") and not getattr(job, "_cli_said", False):
            job._cli_said = True
            msg = {"done": f"finished {job.output}", "failed": f"FAILED: {job.error}",
                   "cancelled": "cancelled"}[job.state]
            print(f"[job] {job.title}: {msg}")

    # ------------------------------------------------------------ loop

    def _status(self):
        cap, rec = self.cap, self.cap.recording
        parts = []
        if rec and not rec.stopping:
            el = rec.elapsed()
            parts.append(("● REC " if int(el * 2) % 2 == 0 else "  REC ") + _fmt_tc(el))
            parts.append(f"{rec.kind} {library._size(rec.size_bytes())}")
            if el > 1:
                parts.append(f"{rec.size_bytes() / el / 1e6:.0f} MB/s")
            st = rec.stats()
            drops = st.get("drops", 0) + st.get("disk_drops", 0)
            if drops:
                parts.append(f"DROPPED {drops}")
            if "buffer" in st:
                parts.append(f"buf {st['buffer'] * 100:.0f}%")
        else:
            mode = "H.265 " + f"{self.s['h265']['bitrate']} Mb/s" if self.s["mode"] == "h265" \
                else "RAW" + "".join(f" +{c.split('-')[0].upper().replace('H265', 'HQ')}"
                                     for c, _ in self._planned_jobs())
            parts.append(f"ready · {mode}")
        parts.append(cap.signal_text() or "no signal")
        try:
            free = shutil.disk_usage(self.s["storage"]).free
            parts.append(f"{library._size(free)} free")
        except OSError:
            pass
        n = self.jobs.active_count()
        if n:
            running = [j for j in self.jobs.jobs if j.state == "running"]
            j = running[0] if running else None
            parts.append(f"jobs {n}" + (f" ({j.frames}/{j.total} {j.fps:.0f}fps)" if j else ""))
        return " | ".join(parts)

    def _tick(self):
        rec = self.cap.recording
        if rec and not rec.stopping:
            try:
                if shutil.disk_usage(self.s["storage"]).free < 1.5e9:
                    rec.stop(on_done=lambda r: self._finished(r, "disk almost full"))
            except OSError:
                pass
            if self.deadline and time.monotonic() >= self.deadline:
                rec.stop(on_done=lambda r: self._finished(r, None))
        if self.args.record and not self.cap.recording and not self.recorded \
                and self.cap.timing and self.cap.pipeline and self.cap.fps_measured:
            self.start()
        if self.args.duration and self.recorded and not self.cap.recording:
            if self.args.no_wait or not self.jobs.active_count():
                self.quit(force=True)
        if isinstance(sys.stdout, StatusOut):
            sys.stdout.set_status(self._status())
        return True

    def _on_key(self, _src, _cond):
        ch = sys.stdin.read(1)
        if ch in ("r", "R", " "):
            self.toggle()
        elif ch in ("m", "M"):
            if self.cap.recording:
                print("[warn] stop recording to change the mode")
            else:
                self.s["mode"] = "raw" if self.s["mode"] == "h265" else "h265"
                print(f"[info] mode: {'RAW' if self.s['mode'] == 'raw' else 'H.265'}")
        elif ch in ("q", "Q", "\x03"):
            self.quit()
        elif ch in ("h", "H", "?"):
            print(HELP)
        return True

    def quit(self, force=False):
        busy = self.cap.recording or self.jobs.active_count()
        if busy and not force and time.monotonic() - self.quit_armed > 4:
            self.quit_armed = time.monotonic()
            print("[warn] a recording / background encode is running — press q again to stop it and quit")
            return
        self.loop.quit()

    def run(self):
        if self.tty:
            self._term = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            GLib.io_add_watch(sys.stdin, GLib.IO_IN, self._on_key)
            sys.stdout = StatusOut(sys.stdout, log.TERMINAL_FD)
            sys.stderr = sys.stdout
            print(HELP)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, lambda: self.quit(force=True) or False)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, lambda: self.quit(force=True) or False)
        self.cap.poll()
        GLib.timeout_add(1000, lambda: self.cap.poll() or True)
        GLib.timeout_add(500, self._tick)
        try:
            self.loop.run()
        finally:
            if self.cap.recording:
                print("[info] finishing the recording…")
            self.cap.stop("quit")
            self.jobs.shutdown()
            if isinstance(sys.stdout, StatusOut):
                out = sys.stdout
                out.set_status("")
                out._tty("bye\n")
                sys.stdout = sys.stderr = out.real
            if self._term:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._term)
            print("bye")


def run(args, main_script):
    # interactive: the status line owns the terminal; GStreamer's own output goes to the log
    log.setup(terminal=not sys.stdin.isatty())
    Cli(args, main_script).run()
