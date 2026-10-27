"""HDMI RX capture process (ARC-06).

Owns the GStreamer pipeline, so a crash in a driver or plugin can never take the
server (Bluetooth sessions, shells) down with it. The server spawns it:

    python3 -m arstro_remote.recorder.worker [--simulate 1920x1080@30] [--device /dev/video0]

  stdin   one JSON command per line:
            {"cmd": "preview", "on": true, "quality": "medium"}
            {"cmd": "keyframe"}
            {"cmd": "record_start", "req": 3, "mode": "h265", "path_base": ".../REC_x",
             "settings": {...}, "audio": true}
            {"cmd": "record_stop", "req": 4}
            {"cmd": "edid", "value": "4k60"}
            {"cmd": "audio_source", "value": "hdmi" | "alsa:<card>"}            (REC-09)
            {"cmd": "monitor", "analysis": true, "lut": {"id", "path"} | null}  (REC-13)
            {"cmd": "zoom", "id": 3, "on": true, "x": 0.5, "y": 0.5, "size": 0.4, "ratio": 3} (REC-14)
            {"cmd": "zoom_keyframe", "id": 3}
            {"cmd": "shutdown"}
  stdout  protocol frames (protocol.py): JSON events {"ev": "caps"|"status"|"stream"|
          "recording_started"|"recording_timecode"|"recording_stopped"|"levels"|"zoom"|"reply", ...},
          VIDEO frames with the preview's H.264 access units and BLOB frames with a zoomed
          viewer's access units ({"z": id, "k": keyframe, "p": pts_us}) or exposure data
          ({"a": 1, grid sizes}, monitor.py). Everything else a library prints goes to stderr.
"""

import argparse
import glob
import json
import os
import shutil
import signal
import sys
import threading
import time

IPC_FD = None
_ipc_lock = threading.Lock()


def _ipc_setup():
    """Keep a private copy of stdout for frames; point fd 1 at stderr so stray output
    (print(), GStreamer's C code) can never corrupt the frame stream."""
    global IPC_FD
    sys.stdout.flush()
    IPC_FD = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr


def _write(data):
    with _ipc_lock:
        view = memoryview(data)
        while view:
            n = os.write(IPC_FD, view)
            view = view[n:]


def _fix_blacklisted_mpp():
    """The Rockchip MPP plugin sometimes lands on GStreamer's blacklist (after a registry
    scan without access to /dev/mpp_service); drop the cache once so it is rescanned."""
    from gi.repository import Gst
    if Gst.ElementFactory.find("mpph265enc") or os.environ.get("ARSTRO_REGISTRY_RESET"):
        return
    if not glob.glob("/usr/lib/*/gstreamer-1.0/libgstrockchipmpp.so"):
        return
    for f in glob.glob(os.path.expanduser("~/.cache/gstreamer-1.0/registry.*.bin")):
        try:
            os.remove(f)
        except OSError:
            pass
    os.environ["ARSTRO_REGISTRY_RESET"] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "arstro_remote.recorder.worker", *sys.argv[1:]])


class Worker:
    TICK_MS = 1000

    def __init__(self, args):
        from gi.repository import GLib, Gst
        from . import v4l2
        from .capture import Capture
        from ..protocol import encode_blob, encode_json, encode_video
        self.GLib, self.Gst = GLib, Gst
        self.encode_json, self.encode_video, self.encode_blob = encode_json, encode_video, encode_blob
        self.v4l2 = v4l2
        self.loop = GLib.MainLoop()
        if args.camera:
            self.sim = v4l2.V4l2Camera(args.camera)
        else:
            self.sim = v4l2.SimulatedSignal(args.simulate) if args.simulate else None
        self.dev = None if self.sim else (args.device or v4l2.find_hdmirx_device())
        self.audio_card = None if self.sim else v4l2.find_hdmiin_audio()
        self.cap = Capture(self.dev, self, audio_card=self.audio_card, simulate=self.sim)
        self.preview_on = False
        self.quality = "medium"
        self.zero_copy = True
        self.pending_record = None
        self.pending_deadline = 0.0
        self.link = (None, None)
        self._link_checked = 0.0
        self.last_status = None
        self.stopping_replies = {}
        self.zoom_wanted = {}             # viewer id -> (x, y, ratio, size): kept across capture restarts
        self.analysis_wanted = False
        self.lut_spec = None

    # --------------------------------------------------------------- output
    def emit(self, **obj):
        try:
            _write(self.encode_json(obj))
        except OSError:
            self.loop.quit()

    def _on_au(self, key, pts, data):
        try:
            _write(self.encode_video(key, pts, data))
        except OSError:
            pass

    def _on_zoom_au(self, zid, key, pts, data):
        try:
            _write(self.encode_blob({"z": zid, "k": key, "p": pts}, data))
        except OSError:
            pass

    def _on_analysis(self, meta, payload):
        try:
            _write(self.encode_blob(dict(meta, a=1), payload))
        except OSError:
            pass

    # ------------------------------------------------------- monitoring events
    def on_levels(self, peak, rms):
        if self.preview_on:
            self.emit(ev="levels", peak=peak, rms=rms)

    def on_zoom(self, zid, error=None):
        self.zoom_wanted.pop(zid, None)
        self.emit(ev="zoom", id=zid, on=False, error=error)

    def on_recording_timecode(self, rec):
        self.emit(ev="recording_timecode", path=rec.path, tc=rec.tc, wall=rec.first_wall)

    def _reattach(self):
        """Capture (re)started: the viewers' zooms and the analysis come back with it."""
        for zid, (x, y, ratio, size) in list(self.zoom_wanted.items()):
            self._zoom(zid, x, y, ratio, size)
        self.cap.set_analysis(self.analysis_wanted and self.preview_on, self._on_analysis)

    def _zoom(self, zid, x, y, ratio, size):
        try:
            z = self.cap.start_zoom(zid, x, y, ratio, size, self._on_zoom_au)
            self.emit(ev="zoom", **z.describe())
        except (RuntimeError, self.GLib.Error) as e:
            self.zoom_wanted.pop(zid, None)
            self.emit(ev="zoom", id=zid, on=False, error=str(e))

    # ------------------------------------------------------- capture events
    def on_signal(self, text):
        print(f"[info] signal: {text or 'none'}")
        self.zero_copy = True          # a new signal may be a format the RGA path handles
        self._status(force=True)

    def on_capture(self, running):
        if running:
            if self.pending_record:
                self._record_when_audio(self.pending_record, time.monotonic() + 1.5)
                self.pending_record = None
            if self.preview_on:
                self._start_stream()
                self._reattach()
        self._status(force=True)

    def on_capture_error(self, message):
        self.emit(ev="capture_error", error=message)
        if self.pending_record:
            self._reply(self.pending_record, error=message)
            self.pending_record = None
        self._status(force=True)

    def on_stream_error(self, st, message):
        if st.path == "zero-copy":
            print("[warn] zero-copy preview failed, falling back to CPU scaling")
            self.zero_copy = False
        self.GLib.timeout_add(500, lambda: (self._start_stream(force=True) if self.preview_on else None) and False)

    def on_recording_stopped(self, rec, reason):
        if getattr(rec, "_reported", False):
            return
        rec._reported = True
        self.emit(ev="recording_stopped", path=rec.path, kind=rec.kind, reason=reason, error=rec.error,
                  size=rec.size_bytes(), duration=round(getattr(rec, "duration", rec.elapsed()), 2),
                  stats=rec.stats())
        for req in self.stopping_replies.pop(id(rec), []):
            self._reply(req, path=rec.path, size=rec.size_bytes(), reason=reason, error=rec.error)
        self._update_wanted()
        self._status(force=True)

    # ------------------------------------------------------------- commands
    def _reply(self, req, error=None, **data):
        self.emit(ev="reply", req=req.get("req"), ok=error is None, error=error, data=data)

    def _update_wanted(self):
        self.cap.set_wanted(self.preview_on or bool(self.cap.recording) or bool(self.pending_record))

    def _start_stream(self, force=False):
        """(Re)build the preview branch. A running branch at the wanted quality is kept:
        several triggers (viewer joined, capture started, retries) must not rebuild the
        encoder again - each rebuild sends viewers a new config and keyframe."""
        if not self.cap.pipeline:
            return
        st = self.cap.stream
        if st is not None and st.bin is not None and st.quality == self.quality and not force:
            return
        try:
            st = self.cap.start_stream(self.quality, self._on_au, zero_copy=self.zero_copy)
            self.emit(ev="stream", **st.describe())
            if st.path == "zero-copy":
                self.GLib.timeout_add(3000, lambda: self._check_zero_copy(st) and False)
        except (RuntimeError, self.GLib.Error) as e:
            print(f"[warn] preview could not start: {e}")
            if self.zero_copy:
                self.zero_copy = False
                self.GLib.timeout_add(300, lambda: (self._start_stream(force=True) if self.preview_on else None) and False)

    def _check_zero_copy(self, st):
        """The RGA path gave no picture in 3 s (a format/size it cannot do): use the CPU path."""
        if self.cap.stream is st and st.frames == 0 and self.cap.pipeline and self.preview_on:
            print(f"[warn] zero-copy preview produced nothing for {self.cap.format}; using the CPU path")
            self.zero_copy = False
            self._start_stream(force=True)

    def handle(self, cmd):
        c = cmd.get("cmd")
        if c == "preview":
            on, q = bool(cmd.get("on")), cmd.get("quality") or self.quality
            changed = q != self.quality
            self.quality = q
            if on and (not self.preview_on or changed or not self.cap.stream):
                self.preview_on = True
                self._update_wanted()          # may start the capture -> on_capture starts the stream
                if self.cap.pipeline:
                    self._start_stream(force=changed)
                    self._reattach()
            elif not on and self.preview_on:
                self.preview_on = False
                self.cap.set_analysis(False)   # nobody looks: a recording alone needs none of it
                for zid in list(self.cap.zooms):
                    self.cap.stop_zoom(zid)
                self.zoom_wanted.clear()
                self.cap.stop_stream()
                self._update_wanted()
            self._status(force=True)
        elif c == "keyframe":
            if self.cap.stream:
                self.cap.stream.force_keyframe()
        elif c == "record_start":
            if self.cap.recording:
                return self._reply(cmd, error="already recording")
            if not self.cap.signal:
                return self._reply(cmd, error="no HDMI signal")
            if self.cap.pipeline and self.cap.timing:
                self._do_record(cmd)
            else:
                self.pending_record = cmd
                self.pending_deadline = time.monotonic() + 10
                self._update_wanted()
        elif c == "record_stop":
            rec = self.cap.recording
            if not rec:
                return self._reply(cmd, error="not recording")
            self.stopping_replies.setdefault(id(rec), []).append(cmd)
            if not rec.stopping:
                rec.stop(on_done=lambda r: self.on_recording_stopped(r, None))
            self._status(force=True)
        elif c == "edid":
            ok = True
            if self.dev and cmd.get("value") in self.v4l2.EDID_TYPES:
                ok = self.v4l2.set_edid(self.dev, cmd["value"])
                if ok:
                    self.v4l2.remember_edid(self.dev, cmd["value"])
            self._reply(cmd, error=None if ok else "could not set the EDID")
        elif c == "audio_source":
            v = cmd.get("value") or "hdmi"
            if v != self.cap.audio_source:
                self.cap.audio_source = v
                self.cap.audio_failed = False
                if self.cap.pipeline and not self.cap.recording:
                    self.cap.restart()
                    if self.preview_on:
                        self._reattach()
            self._reply(cmd, applied=not self.cap.recording)
        elif c == "monitor":
            self.analysis_wanted = bool(cmd.get("analysis"))
            self.cap.set_analysis(self.analysis_wanted and self.preview_on, self._on_analysis)
            spec = cmd.get("lut") or None
            if spec != self.lut_spec:
                self.lut_spec = spec
                self.cap.lut = self._build_lut(spec)
                if self.preview_on and self.cap.pipeline:
                    self._start_stream(force=True)
            self._status(force=True)
        elif c == "zoom":
            zid = int(cmd.get("id", 0))
            if cmd.get("on"):
                x = min(1.0, max(0.0, float(cmd.get("x", 0.5))))
                y = min(1.0, max(0.0, float(cmd.get("y", 0.5))))
                ratio = int(cmd.get("ratio") or 3)
                size = min(1.0, max(0.02, float(cmd.get("size") or 0.4)))
                self.zoom_wanted[zid] = (x, y, ratio, size)
                if self.cap.pipeline and self.cap.timing:
                    self._zoom(zid, x, y, ratio, size)
            else:
                self.zoom_wanted.pop(zid, None)
                self.cap.stop_zoom(zid)
                self.emit(ev="zoom", id=zid, on=False)
        elif c == "zoom_keyframe":
            z = self.cap.zooms.get(int(cmd.get("id", 0)))
            if z:
                z.force_keyframe()
        elif c == "status":
            self._status(force=True)
        elif c == "sim_signal" and self.sim:
            self.sim.present = bool(cmd.get("present", True))
            self.cap.poll()
        elif c == "shutdown":
            self.quit()

    def _record_when_audio(self, cmd, deadline):
        """A take that started the capture (nobody watching: the record button, the CLI) waits
        for the audio's first buffers, or it would be recorded without sound (REC-09, REC-16)."""
        cap = self.cap
        if cap.audio_in_pipeline and not cap.audio_alive() and cap.pipeline and time.monotonic() < deadline:
            self.GLib.timeout_add(50, lambda: self._record_when_audio(cmd, deadline) and False)
            return
        if cap.pipeline:
            self._do_record(cmd)
        else:
            self._reply(cmd, error="capture stopped before the recording could start")

    def _build_lut(self, spec):
        if not spec:
            return None
        from . import monitor
        if not monitor.available():
            print("[warn] LUT needs numpy (python3-numpy)")
            return None
        try:
            if spec.get("path"):
                with open(spec["path"]) as f:
                    return monitor.LutTable.build(spec["id"], cube_text=f.read(), name=spec.get("name"))
            return monitor.LutTable.build(spec["id"])
        except (OSError, ValueError, KeyError) as e:
            print(f"[warn] LUT {spec.get('id')}: {e}")
            self.emit(ev="lut_error", id=spec.get("id"), error=str(e))
            return None

    def _do_record(self, cmd):
        try:
            rec = self.cap.start_recording(cmd["mode"], cmd["path_base"], cmd["settings"],
                                           bool(cmd.get("audio", True)))
        except (RuntimeError, self.GLib.Error) as e:
            self._reply(cmd, error=f"could not start recording: {e}")
            self._update_wanted()
            return
        self.emit(ev="recording_started", path=rec.path, kind=rec.kind, audio=rec.with_audio)
        self._reply(cmd, path=rec.path, kind=rec.kind, audio=rec.with_audio)
        self._status(force=True)

    # ---------------------------------------------------------------- status
    def _signal_info(self):
        sig = self.cap.signal
        d = {"present": bool(sig), "text": self.cap.signal_text(sig) if sig else None,
             "width": sig[0] if sig else 0, "height": sig[1] if sig else 0,
             "fps": round(sig[2], 3) if sig else 0, "format": self.cap.format if sig else None}
        if not sig and self.dev:
            now = time.monotonic()
            if now - self._link_checked > 5:
                self._link_checked = now
                self.link = self.v4l2.link_state(self.dev)
            d["link"], d["why"] = self.link
        elif not sig and not self.dev and not self.sim:
            d["link"], d["why"] = "none", "No HDMI RX device (enable it with setup_hdmirx.sh and reboot)."
        return d

    def status(self):
        cap, rec = self.cap, self.cap.recording
        r = None
        if rec:
            el = rec.elapsed()
            st = rec.stats()
            r = {"active": True, "kind": rec.kind, "path": rec.path, "file": os.path.basename(rec.path),
                 "elapsed": round(el, 1), "size": rec.size_bytes(),
                 "rate": round(rec.size_bytes() / el) if el > 1 else 0,
                 "drops": st.get("drops", 0) + st.get("disk_drops", 0), "buffer": st.get("buffer"),
                 "audio": rec.with_audio, "stopping": rec.stopping,
                 "tc": rec.tc["tc"] if rec.tc else None, "av": st.get("av")}
        return {
            "signal": self._signal_info(),
            "capture": {"running": bool(cap.pipeline), "fps": round(cap.fps_measured, 2),
                        "drops": cap.drops, "audio": cap.has_audio, "error": cap.last_error},
            "stream": cap.stream.describe() if cap.stream else None,
            "preview_on": self.preview_on,
            "monitor": {"analysis": bool(cap.analysis), "zooms": len(cap.zooms),
                        "lut": cap.lut.name if cap.lut else None, "audio_source": cap.audio_source},
            "recording": r,
        }

    def _status(self, force=False):
        st = self.status()
        key = json.dumps(st, sort_keys=True, default=str)
        if force or key != self.last_status:
            self.last_status = key
            self.emit(ev="status", **st)

    def _tick(self):
        rec = self.cap.recording
        if rec and not rec.stopping:
            try:
                if shutil.disk_usage(os.path.dirname(rec.path)).free < 1.5e9:
                    print("[warn] disk almost full - stopping the recording")
                    rec.stop(on_done=lambda r: self.on_recording_stopped(r, "disk almost full"))
            except OSError:
                pass
        if self.pending_record and time.monotonic() > self.pending_deadline:
            self._reply(self.pending_record, error="capture did not start (signal unstable?)")
            self.pending_record = None
            self._update_wanted()
        self._status(force=bool(rec))
        return True

    # ----------------------------------------------------------------- loop
    def _stdin_reader(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            self.GLib.idle_add(lambda c=cmd: self.handle(c) and False)
        self.GLib.idle_add(lambda: self.quit() and False)     # server went away

    def quit(self):
        if self.cap.recording:
            print("[info] finishing the recording before exit")
        self.cap.stop("recorder shutting down")
        self.loop.quit()

    def capabilities(self):
        from . import gpu, monitor
        f = self.Gst.ElementFactory.find
        return {"vpu_h265": bool(f("mpph265enc")), "vpu_h264": bool(f("mpph264enc")),
                "x265": bool(f("x265enc")), "ffv1": bool(f("avenc_ffv1")), "gpu_ffv1": gpu.available(),
                "audio": bool(self.audio_card) or (bool(self.sim) and not getattr(self.sim, "device", None)),
                "device": self.dev or getattr(self.sim, "device", None),
                "simulate": bool(self.sim) and not getattr(self.sim, "device", None),
                "analysis": monitor.available(), "lut": monitor.available(),
                "pcm_mov": bool(f("qtmux")),
                "camera": getattr(self.sim, "device", None)}

    def run(self):
        self.emit(ev="caps", **self.capabilities())
        if self.dev and os.environ.get("ARSTRO_EDID") in self.v4l2.EDID_TYPES:
            self.v4l2.ensure_edid(self.dev, os.environ["ARSTRO_EDID"])
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.GLib.unix_signal_add(self.GLib.PRIORITY_HIGH, sig, lambda: self.quit() or False)
        threading.Thread(target=self._stdin_reader, daemon=True).start()
        self.cap.poll()
        self.GLib.timeout_add(1000, lambda: self.cap.poll() or True)
        self.GLib.timeout_add(self.TICK_MS, self._tick)
        self._status(force=True)
        self.loop.run()


def main():
    ap = argparse.ArgumentParser(prog="arstro_remote.recorder.worker")
    ap.add_argument("--simulate", help="test pattern instead of HDMI RX, e.g. 1920x1080@30")
    ap.add_argument("--device", help="V4L2 device (default: auto-detect hdmirx)")
    ap.add_argument("--camera", help="a V4L2 camera instead of HDMI RX: /dev/videoN[@MJPG 1280x720@30]")
    args = ap.parse_args()
    os.environ["GST_MPP_NO_RGA"] = "0"      # RGA scaling for the zero-copy preview
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    _fix_blacklisted_mpp()                   # may re-exec: must run before _ipc_setup
    _ipc_setup()
    Worker(args).run()


if __name__ == "__main__":
    main()
