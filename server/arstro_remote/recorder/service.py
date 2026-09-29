"""Recorder service (server side): recorder.*, gallery.* and jobs.* ops for every controller.

    controllers ──ops──> RecorderService ──JSON lines──> worker process (GStreamer)
                <─state events── hub <──status/events── worker
    viewers <── Preview <──H.264 access units── worker

Publishes the hub topics "recorder", "recorder.settings", "jobs" and "gallery" (ARC-03).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from ..protocol import T_JSON, T_VIDEO, FrameDecoder, ProtocolError, decode_video
from ..session import OpError
from . import jobs as jobs_mod, library, settings as settings_mod, thumbs
from .preview import Preview

log = logging.getLogger("arstro.recorder")

MIN_FREE_START = 3e9
TARGETS = [   # (id, title, needs capability, description)
    ("h264-vpu", "H.264 MP4 · share", "vpu_h264",
     "Plays everywhere (phones, browsers). Hardware encoder, about real time. Optional downscale."),
    ("h265-vpu", "H.265 · VPU", "vpu_h265", "Small high-quality files. Hardware encoder, about real time."),
    ("h265-x265", "H.265 · x265", "x265", "Best quality per MB. CPU encoder, very slow at 4K."),
    ("ffv1", "FFV1 lossless · CPU", "ffv1", "Mathematically lossless .mkv (~14 fps at 4K)."),
    ("ffv1-gpu", "FFV1 lossless · GPU", "gpu_ffv1", "Same lossless file on the GPU: slower, leaves the CPU free."),
]


class RecorderService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.hub = ctx.hub
        self.simulate = ctx.config.get("recorder_simulate") or os.environ.get("ARSTRO_RECORDER_SIMULATE")
        self.camera = ctx.config.get("camera_device") or None      # a V4L2 camera instead (CAM-02)
        self.settings = settings_mod.load()
        self.library = library.Library()
        self.preview = Preview(self._on_viewers, lambda: self._send({"cmd": "keyframe"}, quiet=True))
        self.jobs = None
        self.proc = None
        self._wlock = threading.Lock()
        self._req = 0
        self._pending = {}
        self.caps = {}
        self.worker_status = {}
        self.recording_path = None
        self.last_recording = None
        self._stopping = False
        self._restarts = 0
        self._gallery = []
        self._gallery_fp = None
        self._gallery_version = 0
        self._gallery_wake = threading.Event()
        self._planned = {}
        self._raw_to_delete = {}       # RAW path -> job whose FFV1 copy was verified (GAL-08)

    # ------------------------------------------------------------ lifecycle
    @property
    def storage(self):
        return settings_mod.storage_dir(self.settings)

    def start(self):
        os.makedirs(self.storage, exist_ok=True)
        self.jobs = jobs_mod.JobManager(self._on_job)      # GLib io watches: main thread
        self.hub.publish("recorder.settings", self.settings)
        self.hub.publish("jobs", [])
        self._spawn_worker()
        threading.Thread(target=self._gallery_loop, name="gallery", daemon=True).start()

    def shutdown(self):
        self._stopping = True
        self.preview.close_all()
        if self.proc and self.proc.poll() is None:
            self._send({"cmd": "shutdown"}, quiet=True)
            try:
                self.proc.wait(25)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.jobs:
            self.jobs.shutdown()

    def _log_file(self):
        from ..paths import state_dir
        os.makedirs(state_dir(), exist_ok=True)
        p = os.path.join(state_dir(), "recorder.log")
        try:
            if os.path.getsize(p) > 5_000_000:
                os.replace(p, p + ".1")
        except OSError:
            pass
        return open(p, "ab")

    def _spawn_worker(self):
        pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = dict(os.environ, PYTHONUNBUFFERED="1", ARSTRO_EDID=self.settings.get("edid", "keep"))
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        cmd = [sys.executable, "-m", "arstro_remote.recorder.worker"]
        if self.camera:
            cmd += ["--camera", self.camera]
        elif self.simulate:
            cmd += ["--simulate", self.simulate]
        logf = self._log_file()
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=logf, bufsize=0, env=env)
        finally:
            logf.close()
        log.info("recorder worker started (pid %d, source %s)", self.proc.pid, self.source_id())
        threading.Thread(target=self._reader, args=(self.proc,), name="recorder-rx", daemon=True).start()
        if self.preview.count:
            self._send({"cmd": "preview", "on": True, "quality": self._quality()}, quiet=True)

    def _reader(self, proc):
        dec = FrameDecoder()
        fd = proc.stdout.fileno()
        try:
            while True:
                data = os.read(fd, 1 << 20)
                if not data:
                    break
                for ftype, payload in dec.feed(data):
                    if ftype == T_VIDEO:
                        key, pts, au = decode_video(payload)
                        self.preview.frame(key, pts, au)
                    elif ftype == T_JSON:
                        try:
                            self._on_event(json.loads(payload))
                        except Exception:
                            log.exception("recorder event")
        except (OSError, ProtocolError) as e:
            log.error("recorder worker stream: %s", e)
        code = proc.wait()
        if self._stopping or proc is not self.proc:
            return
        log.error("recorder worker exited (code %s)", code)
        for req, (ev, box) in list(self._pending.items()):
            box["error"] = "recorder process exited"
            ev.set()
        if self.recording_path:
            self.last_recording = {"file": os.path.basename(self.recording_path),
                                   "reason": "capture process crashed", "error": "worker exit %s" % code}
            self.recording_path = None
        self.worker_status = {}
        self.preview.set_state("stopped", "recorder restarting")
        self._publish_status()
        self._gallery_wake.set()
        self._restarts += 1
        delay = min(30, 2 ** min(self._restarts, 5))
        threading.Timer(delay, self._spawn_worker).start()

    # --------------------------------------------------------- worker events
    def _on_event(self, ev):
        kind = ev.pop("ev", None)
        if kind == "caps":
            self.caps = ev
            self._restarts = 0
        elif kind == "status":
            self.worker_status = ev
            rec = ev.get("recording")
            self.recording_path = rec["path"] if rec else None
            sig = ev.get("signal") or {}
            if not sig.get("present"):
                self.preview.set_state("no-signal", sig.get("why") or "No HDMI signal")
            elif not ev.get("stream"):
                self.preview.set_state("starting")      # never leave a stale "no-signal" behind
        elif kind == "stream":
            self.preview.set_config({k: ev.get(k) for k in ("width", "height", "fps", "bitrate", "quality")})
        elif kind == "recording_started":
            self.recording_path = ev["path"]
            planned = self._planned.pop("next", [])
            if self.settings["mode"] == "raw" and self.settings["raw"]["when"] == "during":
                for codec, opts in planned:
                    self._main(self.jobs.add, ev["path"], codec, opts, True)
            else:
                self._planned[ev["path"]] = planned
            self._gallery_wake.set()
        elif kind == "recording_stopped":
            path = ev.get("path")
            self.last_recording = {"file": os.path.basename(path or ""), "reason": ev.get("reason"),
                                   "error": ev.get("error"), "size": ev.get("size"),
                                   "duration": ev.get("duration")}
            self.recording_path = None
            for codec, opts in self._planned.pop(path, []):
                if ev.get("size") and path and os.path.exists(path):
                    self._main(self.jobs.add, path, codec, opts, False)
            self._gallery_wake.set()
            log.info("recording finished: %s", self.last_recording)
        elif kind == "reply":
            box = self._pending.pop(ev.get("req"), None)
            if box:
                if not ev.get("ok"):
                    box[1]["error"] = ev.get("error") or "failed"
                box[1]["data"] = ev.get("data") or {}
                box[0].set()
            return
        elif kind == "capture_error":
            self.preview.set_state("error", ev.get("error"))
        self._publish_status()

    # ---------------------------------------------------------- worker I/O
    def _send(self, obj, quiet=False):
        proc = self.proc
        if not proc or proc.poll() is not None:
            if quiet:
                return
            raise OpError("recorder process not running")
        line = (json.dumps(obj) + "\n").encode()
        with self._wlock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except OSError:
                if not quiet:
                    raise OpError("recorder process not reachable")

    def _request(self, obj, timeout):
        with self._wlock:
            self._req += 1
            req = self._req
        ev, box = threading.Event(), {}
        self._pending[req] = (ev, box)
        self._send(dict(obj, req=req))
        if not ev.wait(timeout):
            self._pending.pop(req, None)
            raise OpError("recorder did not answer (%s)" % obj.get("cmd"))
        if box.get("error"):
            raise OpError(box["error"])
        return box.get("data") or {}

    def _main(self, fn, *args):
        return self.ctx.call_in_main(fn, *args)

    def _on_viewers(self, n):
        self._send({"cmd": "preview", "on": n > 0, "quality": self._quality()}, quiet=True)
        if n == 0:
            self.preview.set_state("stopped")
        self._publish_status()

    def _quality(self):
        return self.settings.get("preview", {}).get("quality", "medium")

    # --------------------------------------------------------------- status
    def status(self):
        ws = self.worker_status or {}
        try:
            du = shutil.disk_usage(self.storage)
            disk = {"path": self.storage, "free": du.free, "total": du.total}
        except OSError:
            disk = {"path": self.storage, "free": 0, "total": 0}
        s = self.settings
        mode = ("H.265 %d Mb/s %s" % (s["h265"]["bitrate"], s["h265"]["container"].upper())
                if s["mode"] == "h265" else "RAW" + "".join(
                    " +" + {"h265-vpu": "HQ", "h265-x265": "HQ", "ffv1": "FFV1", "ffv1-gpu": "FFV1"}[c]
                    for c, _ in self._planned_jobs()))
        return {
            "available": bool(self.proc and self.proc.poll() is None and self.caps),
            "caps": self.caps,
            "signal": ws.get("signal") or {"present": False, "why": "recorder starting"},
            "capture": ws.get("capture") or {"running": False},
            "recording": ws.get("recording") or {"active": False},
            "preview": {"viewers": self.preview.count, "on": bool(ws.get("preview_on")),
                        "stream": ws.get("stream"), "quality": self._quality(), "path": "/ws/preview",
                        "state": self.preview.state.get("state")},
            "disk": disk,
            "mode": s["mode"], "mode_text": mode,
            "last": self.last_recording,
            "simulate": None if self.camera else self.simulate,
            "camera": self.camera,
            "source": self.source_id(),
        }

    def _publish_status(self):
        self.hub.publish("recorder", self.status())

    def _planned_jobs(self):
        r, caps = self.settings["raw"], self.caps
        out = []
        if self.settings["mode"] != "raw":
            return out
        if r["hq"]:
            codec = "h265-x265" if r["hq_engine"] == "x265" or not caps.get("vpu_h265") else "h265-vpu"
            out.append((codec, {"quality": r["hq_quality"], "preset": r["x265_preset"],
                                "chroma": r["hq_chroma"]}))
        replace = {"verify": bool(r.get("ffv1_replace_raw")), "replace_raw": bool(r.get("ffv1_replace_raw"))}
        if r["ffv1"] and r.get("ffv1_engine") == "gpu" and caps.get("gpu_ffv1"):
            out.append(("ffv1-gpu", dict(replace)))
        elif r["ffv1"] and caps.get("ffv1"):
            out.append(("ffv1", dict(replace)))
        return out

    # -------------------------------------------------------------- gallery
    def _scan(self):
        busy = self.jobs.busy_paths() if self.jobs else set()
        return self.library.scan(self.storage, self.recording_path, busy)

    def _gallery_loop(self):
        while True:
            try:
                takes = self._scan()
                fp = json.dumps([(t["id"], [(i["id"], i["kind"], i["busy"], i["problem"],
                                            None if i["recording"] else (i["size"], i["mtime"]))
                                           for i in t["items"]]) for t in takes])
                self._gallery = takes
                if fp != self._gallery_fp:
                    self._gallery_fp = fp
                    self._gallery_version += 1
                    self.hub.publish("gallery", {
                        "version": self._gallery_version, "takes": len(takes),
                        "files": sum(len(t["items"]) for t in takes),
                        "size": sum(t["size"] for t in takes), "folder": self.storage})
            except Exception:
                log.exception("gallery scan")
            self._gallery_wake.wait(5)
            self._gallery_wake.clear()

    def refresh_gallery(self):
        self._gallery_wake.set()

    def _take(self, take_id):
        for t in self._gallery or self._scan():
            if t["id"] == take_id:
                return t
        for t in self._scan():
            if t["id"] == take_id:
                return t
        raise OpError("no take %s" % take_id)

    def _file_path(self, name):
        if not name or "/" in name or name.startswith(".") or not name.lower().endswith(library.EXTENSIONS):
            raise OpError("bad file name %r" % name)
        p = os.path.join(self.storage, name)
        if not os.path.isfile(p):
            raise OpError("no file %s" % name)
        return p

    def media_path(self, name):
        try:
            return self._file_path(name)
        except OpError:
            return None

    def thumbnail(self, take_id):
        try:
            return thumbs.get(self.storage, self._take(take_id))
        except OpError:
            return None

    @staticmethod
    def _remove_sidecars(path):
        for suffix in (jobs_mod.VERIFIED_SUFFIX, ".verify.log"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass

    def _check_deletable(self, path):
        if self.recording_path and os.path.abspath(path) == os.path.abspath(self.recording_path):
            raise OpError("%s is being recorded" % os.path.basename(path))
        if os.path.abspath(path) in {os.path.abspath(p) for p in (self.jobs.busy_paths() if self.jobs else ())}:
            raise OpError("%s is being converted - cancel the job first" % os.path.basename(path))

    # ------------------------------------------------------------------ jobs
    def _on_job(self, job):
        if job is not None and job.finished and (job.verify or {}).get("ok") and job.opts.get("replace_raw"):
            self._raw_to_delete[job.src] = job
        self._delete_verified_raws()
        self.hub.publish("jobs", [j.describe() for j in self.jobs.jobs])
        if job is None or job.finished or job.state in ("running", "verifying"):
            self._gallery_wake.set()

    def _delete_verified_raws(self):
        """GAL-08: a RAW whose FFV1 copy was proven identical goes - once nothing else needs it."""
        for src, job in list(self._raw_to_delete.items()):
            others = [j for j in self.jobs.jobs if j is not job and not j.finished and j.src == src]
            if others or (self.recording_path and os.path.abspath(src) == os.path.abspath(self.recording_path)):
                continue                                  # another job still reads it: later
            del self._raw_to_delete[src]
            if not os.path.exists(job.output) or not os.path.exists(job.output + jobs_mod.VERIFIED_SUFFIX):
                log.warning("not deleting %s: its verified copy %s is gone", src, job.output)
                continue
            try:
                os.remove(src)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("could not delete %s: %s", src, e)
                job.verify["raw_error"] = str(e)
                continue
            self.library.forget(src)
            thumbs.drop(library.clip_id(os.path.basename(src)))
            job.verify["raw_deleted"] = True
            log.info("deleted RAW %s: %s is a verified lossless copy (%s)", os.path.basename(src),
                     os.path.basename(job.output), job.verify.get("detail"))

    # ------------------------------------------------------------------- ops
    def handle(self, session, op, msg):
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            raise OpError("unknown op %s" % op)
        return fn(session, msg)

    def op_recorder_status(self, _s, _m):
        return self.status()

    def op_recorder_start(self, session, _m):
        st = self.status()
        if not st["available"]:
            raise OpError("recorder is not running")
        if st["recording"].get("active"):
            raise OpError("already recording")
        if not st["signal"].get("present"):
            raise OpError("no HDMI signal" + (": " + st["signal"]["why"] if st["signal"].get("why") else ""))
        folder = self.storage
        os.makedirs(folder, exist_ok=True)
        free = shutil.disk_usage(folder).free
        if free < MIN_FREE_START:
            raise OpError("not enough space in %s (%s free, need 3 GB)" % (folder, library.fmt_size(free)))
        s = self.settings
        mode = s["mode"]
        base = os.path.join(folder, datetime.now().strftime("REC_%Y%m%d_%H%M%S"))
        self._planned["next"] = self._planned_jobs()
        log.info("recording requested by session %d (%s): %s", session.num, session.controller, mode)
        data = self._request({"cmd": "record_start", "mode": mode, "path_base": base,
                              "settings": s["h265"] if mode == "h265" else s["raw"],
                              "audio": s["audio"]["record"]}, timeout=20)
        self._publish_status()
        return dict(data, file=os.path.basename(data.get("path", "")))

    def op_recorder_stop(self, session, _m):
        log.info("stop requested by session %d (%s)", session.num, session.controller)
        data = self._request({"cmd": "record_stop"}, timeout=45)
        self._publish_status()
        return dict(data, file=os.path.basename(data.get("path") or ""))

    def source_id(self):
        if self.camera:
            return "v4l2:" + self.camera
        return "test:" + self.simulate if self.simulate else "hdmi"

    def _switch_source(self, session, camera=None, simulate=None):
        """Restart only the worker with another source (REC-08, CAM-01/02)."""
        if (self.worker_status.get("recording") or {}).get("active"):
            raise OpError("stop the recording first")
        label = ("camera " + camera) if camera else ("test pattern " + simulate) if simulate else "HDMI RX"
        log.info("camera source -> %s (session %d, %s)", label, session.num, session.controller)
        old, self.proc = self.proc, None
        if old and old.poll() is None:
            try:
                old.stdin.write(b'{"cmd": "shutdown"}\n')
                old.stdin.flush()
                old.wait(15)
            except (OSError, subprocess.TimeoutExpired):
                old.kill()
        self.camera, self.simulate = camera, simulate
        self.caps, self.worker_status = {}, {}
        self._spawn_worker()
        deadline = time.monotonic() + 10
        while not self.caps and time.monotonic() < deadline:
            time.sleep(0.1)
        try:
            from ..paths import save_config_values
            save_config_values({"camera_device": camera, "recorder_simulate": simulate})
        except OSError as e:
            log.warning("could not save the camera source: %s", e)
        self._publish_status()
        return self.status()

    def op_recorder_source(self, session, msg):
        """Switch between HDMI RX and a test pattern (REC-08); restarts only the worker."""
        import re
        spec = msg.get("simulate") or None
        if spec and not re.fullmatch(r"\d{2,4}x\d{2,4}@\d{1,3}(\.\d+)?(:\w+)?(:[\w-]+)?", spec):
            raise OpError("simulate must look like 1280x720@30")
        return self._switch_source(session, None, spec)

    def op_camera_sources(self, _s, _m):
        from . import v4l2
        hdmi = v4l2.find_hdmirx_device()
        sources = [{"id": "hdmi", "kind": "hdmi", "title": "HDMI input", "device": hdmi, "available": bool(hdmi),
                    "note": None if hdmi else "no HDMI receiver on this machine"}]
        for cam in v4l2.list_cameras():
            sources.append({"id": "v4l2:" + cam["device"], "kind": "v4l2", "title": cam["name"],
                            "device": cam["device"], "available": True, "modes": cam["modes"],
                            "note": "%s · %s" % (cam["driver"], cam["bus"])})
        if self.camera and not any(s_.get("device") == self.camera.split("@")[0] for s_ in sources):
            sources.append({"id": "v4l2:" + self.camera, "kind": "v4l2", "title": self.camera,
                            "device": self.camera.split("@")[0], "available": False, "note": "not connected"})
        sources.append({"id": "test", "kind": "test", "title": "Test pattern", "available": True,
                        "note": "a moving pattern with a tick sound, for trying things out"})
        return {"sources": sources, "current": self.source_id()}

    def op_camera_select(self, session, msg):
        """source: "hdmi" | "test" (+ spec "1920x1080@30") | "v4l2:/dev/videoN" (+ mode "MJPG 1280x720@30")."""
        import re
        src = str(msg.get("source") or "")
        if src == "hdmi":
            return self._switch_source(session, None, None)
        if src.startswith("test"):
            spec = msg.get("spec") or src.partition(":")[2] or "1920x1080@30"
            if not re.fullmatch(r"\d{2,4}x\d{2,4}@\d{1,3}(\.\d+)?(:\w+)?(:[\w-]+)?", spec):
                raise OpError("the test pattern size must look like 1280x720@30")
            return self._switch_source(session, None, spec)
        if src.startswith("v4l2:"):
            dev = src[5:].split("@")[0]
            if not re.fullmatch(r"/dev/video\d+", dev):
                raise OpError("camera must look like v4l2:/dev/video2")
            if not os.path.exists(dev):
                raise OpError("%s is not connected" % dev)
            mode = msg.get("mode")
            if mode and not re.fullmatch(r"\w+ \d+x\d+@[\d.]+", mode):
                raise OpError("mode must look like 'MJPG 1280x720@30'")
            return self._switch_source(session, dev + ("@" + mode if mode else ""), None)
        raise OpError("unknown camera source %r" % src)

    def op_recorder_test_signal(self, _s, msg):
        """Test pattern only: pretend the HDMI signal went away / came back (REC-05 tests)."""
        if not self.simulate:
            raise OpError("only available with a test source (recorder.source)")
        self._send({"cmd": "sim_signal", "present": bool(msg.get("present", True))})
        time.sleep(1.5)
        return self.status()

    def op_recorder_settings_get(self, _s, _m):
        return self.settings

    def op_recorder_settings_set(self, session, msg):
        patch = msg.get("settings") or msg.get("patch")
        old = self.settings
        new = settings_mod.apply_patch(patch)
        self.settings = new
        log.info("recorder settings changed by session %d (%s): %s", session.num, session.controller, patch)
        self.hub.publish("recorder.settings", new)
        if new["preview"]["quality"] != old["preview"]["quality"] and self.preview.count:
            self._send({"cmd": "preview", "on": True, "quality": self._quality()}, quiet=True)
        if new["edid"] != old["edid"]:
            self._send({"cmd": "edid", "value": new["edid"]}, quiet=True)
        if new["storage"] != old["storage"]:
            self._gallery_wake.set()
        self._publish_status()
        return new

    def op_recorder_edid(self, session, msg):
        return self.op_recorder_settings_set(session, {"settings": {"edid": msg.get("value")}})

    def op_recorder_preview_quality(self, session, msg):
        return self.op_recorder_settings_set(session, {"settings": {"preview": {"quality": msg.get("quality")}}})

    def op_gallery_list(self, _s, msg):
        takes = self._scan()
        self._gallery = takes
        kind = msg.get("kind")
        if kind and kind != "all":
            takes = [dict(t, items=[i for i in t["items"] if i["kind"] == kind]) for t in takes]
            takes = [t for t in takes if t["items"]]
        return {"folder": self.storage, "version": self._gallery_version, "takes": takes}

    def op_gallery_get(self, _s, msg):
        return self._take(msg.get("take"))

    def op_gallery_targets(self, _s, _m):
        return {"targets": [{"id": tid, "title": title, "available": bool(self.caps.get(cap)),
                             "description": desc,
                             "options": {"quality": ["high", "higher", "max"],
                                         "bitrate": tid.startswith("h26"),
                                         "scale": list(jobs_mod.SCALES) if tid == "h264-vpu" else None,
                                         "preset": jobs_mod.X265_PRESETS if tid == "h265-x265" else None}}
                            for tid, title, cap, desc in TARGETS]}

    def op_gallery_convert(self, session, msg):
        path = self._file_path(msg.get("file"))
        target = msg.get("target")
        tmap = {t[0]: t for t in TARGETS}
        if target not in tmap:
            raise OpError("unknown target %r (one of %s)" % (target, ", ".join(tmap)))
        if not self.caps.get(tmap[target][2]):
            raise OpError("%s is not available on this board" % tmap[target][1])
        if self.recording_path and os.path.abspath(path) == os.path.abspath(self.recording_path):
            raise OpError("wait until the recording has finished")
        opts = {"quality": msg.get("quality") or "high", "preset": msg.get("preset") or "fast",
                "chroma": msg.get("chroma") or "420", "scale": msg.get("scale") or "source"}
        if opts["quality"] not in jobs_mod.X265_CRF:
            raise OpError("quality must be high, higher or max")
        if opts["scale"] not in jobs_mod.SCALES:
            raise OpError("scale must be source, 1080 or 720")
        if msg.get("bitrate"):
            opts["bitrate"] = float(msg["bitrate"])
            opts["rc"] = msg.get("rc") or "vbr"
        if target in ("ffv1", "ffv1-gpu") and path.lower().endswith(".arh"):
            replace = msg.get("replace_raw")
            replace = self.settings["raw"].get("ffv1_replace_raw") if replace is None else bool(replace)
            opts["verify"] = opts["replace_raw"] = bool(replace)
        out = jobs_mod.output_path(path, target, opts["scale"])
        if os.path.abspath(out) == os.path.abspath(path):
            raise OpError("the file already is in that format")
        job = self._main(self.jobs.add, path, target, opts, False)
        log.info("conversion %s -> %s requested by session %d (%s)", os.path.basename(path), target,
                 session.num, session.controller)
        return job.describe()

    def op_gallery_verify(self, session, msg):
        """Check a take's FFV1 copy against its RAW byte for byte; optionally delete the RAW."""
        path = self._file_path(msg.get("file"))
        take = self._take(library.clip_id(os.path.basename(path)))
        items = take["items"]
        raw = next((i for i in items if i["kind"] == "RAW"), None)
        ffv1 = next((i for i in items if i["kind"] == "FFV1"), None)
        if not raw or not ffv1:
            raise OpError("this take needs both a RAW and an FFV1 file to compare")
        if raw.get("recording") or ffv1.get("busy") or raw.get("busy"):
            raise OpError("wait until the recording / conversion of this take has finished")
        delete = bool(msg.get("delete_raw"))
        job = self._main(self.jobs.add, os.path.join(self.storage, raw["id"]), "verify",
                         {"verify": True, "replace_raw": delete}, False, os.path.join(self.storage, ffv1["id"]))
        log.info("lossless check of %s against %s requested by session %d (%s)%s", ffv1["id"], raw["id"],
                 session.num, session.controller, ", then delete the RAW" if delete else "")
        return job.describe()

    def op_gallery_delete(self, session, msg):
        path = self._file_path(msg.get("file"))
        self._check_deletable(path)
        os.remove(path)
        self._remove_sidecars(path)
        self.library.forget(path)
        thumbs.drop(library.clip_id(os.path.basename(path)))
        log.info("deleted %s (session %d, %s)", path, session.num, session.controller)
        self._gallery_wake.set()
        return {"deleted": [os.path.basename(path)]}

    def op_gallery_delete_take(self, session, msg):
        take = self._take(msg.get("take"))
        paths = [os.path.join(self.storage, i["id"]) for i in take["items"]]
        for p in paths:
            self._check_deletable(p)
        done = []
        for p in paths:
            try:
                os.remove(p)
                self._remove_sidecars(p)
                self.library.forget(p)
                done.append(os.path.basename(p))
            except FileNotFoundError:
                pass
        thumbs.drop(take["id"])
        log.info("deleted take %s (session %d, %s)", take["id"], session.num, session.controller)
        self._gallery_wake.set()
        return {"deleted": done}

    def op_jobs_list(self, _s, _m):
        return {"jobs": [j.describe() for j in self.jobs.jobs]}

    def op_jobs_cancel(self, _s, msg):
        job = self.jobs.get(msg.get("id"))
        if not job:
            raise OpError("no job %s" % msg.get("id"))
        self._main(self.jobs.cancel, job)
        return job.describe()

    def op_jobs_clear(self, _s, _m):
        self._main(self.jobs.clear_finished)
        return {"jobs": [j.describe() for j in self.jobs.jobs]}
