"""Remote screen service (SCR-01..05): runs the capture worker while someone watches.

Viewers connect to the web server's /ws/screen (same messages as /ws/preview, see
recorder/preview.py). Control goes through the normal input ops (in.move_to, in.btn,
in.scroll, in.key, in.text), so every controller can drive the Pi from the picture.

State topic "screen": {available, state, viewers, screen: [w, h], stream: {...},
quality, encoder, bitrate, error}.
"""

import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time

from ..paths import config_dir, state_dir
from ..recorder.preview import Preview

log = logging.getLogger("arstro.screen")

QUALITIES = ("low", "medium", "high")
ENCODERS = ("auto", "vpu", "cpu")             # auto = the VPU when there is one (SCR-05)
BITRATE_RANGE = (200_000, 20_000_000)         # bits/s; 0 = from the quality
DEFAULTS = {"quality": "medium", "encoder": "auto", "bitrate": 0}
FRAME = struct.Struct(">BQI")
SIZE = struct.Struct(">I")


class OpError(Exception):
    pass


def _settings_path():
    return os.path.join(config_dir(), "screen.json")


def validate(new, base=DEFAULTS):
    """base + the given keys, checked -> settings, or ValueError with a sentence for the user."""
    out = dict(base)
    if "quality" in new:
        if new["quality"] not in QUALITIES:
            raise ValueError("quality must be low, medium or high")
        out["quality"] = new["quality"]
    if "encoder" in new:
        if new["encoder"] not in ENCODERS:
            raise ValueError("encoder must be auto, vpu or cpu")
        out["encoder"] = new["encoder"]
    if "bitrate" in new:
        try:
            b = int(new["bitrate"] or 0)
        except (TypeError, ValueError):
            raise ValueError("bitrate must be a number of bits/s (0 = from the quality)")
        if b and not BITRATE_RANGE[0] <= b <= BITRATE_RANGE[1]:
            raise ValueError("bitrate must be 0 (from the quality) or %d k - %d M bits/s"
                             % (BITRATE_RANGE[0] // 1000, BITRATE_RANGE[1] // 1_000_000))
        out["bitrate"] = b
    return out


def load_settings():
    try:
        with open(_settings_path()) as f:
            s = json.load(f)
        out = dict(DEFAULTS)
        for k in DEFAULTS:                     # one bad key does not lose the others
            try:
                out = validate({k: s[k]}, out)
            except (KeyError, ValueError):
                pass
        return out
    except (OSError, ValueError, AttributeError):
        return dict(DEFAULTS)


class ScreenService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.hub = ctx.hub
        self.display = os.environ.get("DISPLAY") or ""
        self.settings = load_settings()
        self.preview = Preview(self._on_viewers, self._keyframe, name="screen")
        self.proc = None
        self.lock = threading.Lock()
        self.stream = None
        self.screen = None
        self.error = None
        self.state = "stopped"
        self._restarts = 0
        self._wanted = False

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self._publish()

    def shutdown(self):
        self._wanted = False
        self._stop_worker()
        self.preview.close_all()

    def _on_viewers(self, n):
        if n > 0 and not self._wanted:
            self._wanted = True
            self._restarts = 0
            self._spawn()
        elif n == 0 and self._wanted:
            self._wanted = False
            self._stop_worker()
            self.preview.set_state("stopped")
            self.state = "stopped"
        self._publish()

    def _spawn(self):
        if not self.display:
            self._fail("no desktop display (DISPLAY is not set for the server)")
            return
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            env = dict(os.environ)
            env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            logf = open(os.path.join(state_dir(), "screen.log"), "a")
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-m", "arstro_remote.screen.worker", "--quality", self.settings["quality"],
                     "--encoder", self.settings["encoder"], "--bitrate", str(self.settings["bitrate"]),
                     "--display", self.display],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf, env=env, bufsize=0)
            except OSError as e:
                logf.close()
                self._fail("could not start the screen capture: %s" % e)
                return
            logf.close()
            proc = self.proc
        self.error = None
        self.state = "starting"
        self.preview.set_state("starting")
        log.info("screen capture started (pid %d, %s, %s, encoder %s, bitrate %s)", proc.pid, self.display,
                 self.settings["quality"], self.settings["encoder"], self.settings["bitrate"] or "auto")
        threading.Thread(target=self._reader, args=(proc,), name="screen-reader", daemon=True).start()

    def _stop_worker(self):
        with self.lock:
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.stream = None

    def _reader(self, proc):
        read = proc.stdout.read
        try:
            while True:
                kind = read(1)
                if not kind:
                    break
                if kind == b"F":
                    key, pts, n = FRAME.unpack(_exact(read, FRAME.size))
                    self.preview.frame(bool(key), pts, _exact(read, n))
                    if self.state != "live":
                        self.state = "live"
                        self._restarts = 0
                        self._publish()
                else:
                    (n,) = SIZE.unpack(_exact(read, SIZE.size))
                    msg = json.loads(_exact(read, n))
                    if kind == b"C":
                        self.screen = msg.get("screen")
                        self.stream = {k: msg.get(k) for k in ("width", "height", "fps", "bitrate", "quality", "encoder")}
                        self.preview.set_config(self.stream)
                        self._publish()
                    elif kind == b"E":
                        self.error = msg.get("error")
        except (OSError, ValueError, struct.error, EOFError):
            pass
        code = proc.wait()
        with self.lock:
            if self.proc is not proc:
                return                       # stopped on purpose / replaced
            self.proc = None
        self.stream = None
        if self._wanted:
            self._restarts += 1
            delay = min(10, 2 ** min(self._restarts, 3))
            self._fail(self.error or "the screen capture stopped (code %s)" % code, retry=delay)
        else:
            self.state = "stopped"
            self._publish()

    def _fail(self, message, retry=None):
        self.error = message
        self.state = "error"
        log.warning("screen: %s%s", message, " - retrying in %ds" % retry if retry else "")
        self.preview.set_state("no-signal", message)
        self._publish()
        if retry:
            threading.Timer(retry, lambda: self._wanted and self._spawn()).start()

    def _keyframe(self):
        with self.lock:
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b'{"cmd": "keyframe"}\n')
                proc.stdin.flush()
            except OSError:
                pass

    # --------------------------------------------------------------- state
    def status(self):
        return {"available": bool(self.display), "display": self.display, "state": self.state,
                "viewers": self.preview.count, "screen": self.screen, "stream": self.stream,
                "quality": self.settings["quality"], "encoder": self.settings["encoder"],
                "bitrate": self.settings["bitrate"], "error": self.error, "path": "/ws/screen"}

    def _publish(self):
        self.hub.publish("screen", self.status())

    # ----------------------------------------------------------------- ops
    def handle(self, session, op, msg):
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            raise OpError("unknown op %s" % op)
        return fn(session, msg)

    def op_screen_status(self, _s, _m):
        return self.status()

    def op_screen_settings_set(self, session, msg):
        new = {k: v for k, v in (msg.get("settings") or msg).items() if k in DEFAULTS}
        if not new:
            raise OpError("settings needs quality, encoder or bitrate")
        try:
            self.settings = validate(new, self.settings)
        except ValueError as e:
            raise OpError(str(e))
        os.makedirs(config_dir(), exist_ok=True)
        with open(_settings_path(), "w") as f:
            json.dump(self.settings, f)
        log.info("screen settings %s (session %d, %s)", new, session.num, session.controller)
        if self._wanted:                                   # restart with the new size / rate
            self._stop_worker()
            self._spawn()
        self._publish()
        return self.status()


def _exact(read, n):
    out = b""
    while len(out) < n:
        chunk = read(n - len(out))
        if not chunk:
            raise EOFError
        out += chunk
    return out
