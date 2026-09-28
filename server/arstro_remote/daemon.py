"""Arstro Remote daemon: Bluetooth RFCOMM server + local control socket."""

import fcntl
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time

from . import __version__
from .input_x11 import create_input
from .session import Session
from .stats import StatsCollector
from .terminal import TerminalPool

log = logging.getLogger("arstro")

DEFAULT_CONFIG = {
    # Bluetooth name shown to phones. {hostname} is replaced. null keeps the current name.
    "alias": "Arstro-{hostname}",
    # "window": pairing allowed for pair_window_sec after start (and after `arstro-remote pair`)
    # "always": always discoverable and pairable, "never": only already-paired phones
    "pairing": "window",
    "pair_window_sec": 600,
    # Preferred RFCOMM channel (a free one is chosen if it is taken)
    "channel": 22,
    # Adapter name (e.g. "hci0"); null = first adapter
    "adapter": None,
    # How long a phone's shells survive after the Bluetooth link drops
    "term_keep_sec": 600,
}


def config_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "arstro-remote")


def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "arstro-remote")


def runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


def control_socket_path():
    return os.path.join(runtime_dir(), "arstro-remote.sock")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(config_dir(), "config.json")
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print("arstro-remote: ignoring bad config %s: %s" % (path, e), file=sys.stderr)
    return cfg


def setup_logging(debug=False):
    os.makedirs(state_dir(), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(os.path.join(state_dir(), "arstro-remote.log"),
                                              maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr.isatty() or os.environ.get("ARSTRO_LOG_STDERR"):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


class Daemon:
    def __init__(self, config, use_bluetooth=True):
        self.config = config
        self.use_bluetooth = use_bluetooth
        self.started = time.time()
        self.stats = StatsCollector()
        self.input = create_input()
        self.terms = TerminalPool(int(config.get("term_keep_sec", 600)))
        self.sessions = set()
        self._lock = threading.Lock()
        self.bt = None
        self.loop = None
        self._lock_file = None

    # ------------------------------------------------------ main-loop helpers
    def call_in_main(self, fn, *args, timeout=15):
        """Run fn on the GLib loop thread (required for dbus-python) and wait."""
        if self.loop is None or threading.current_thread() is threading.main_thread():
            return fn(*args)
        from gi.repository import GLib
        done = threading.Event()
        box = {}

        def run():
            try:
                box["value"] = fn(*args)
            except Exception as e:
                box["error"] = e
            done.set()
            return False

        GLib.idle_add(run)
        if not done.wait(timeout):
            raise RuntimeError("main loop busy")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # -------------------------------------------------------------- sessions
    def _add_session(self, sock, peer, local, device_path=None):
        session = Session(sock, self, peer, local=local)
        session.device_path = device_path
        with self._lock:
            self.sessions.add(session)
        log.info("session %d started: %s%s", session.num, peer, " (local)" if local else "")
        session.start()
        return session

    def session_closed(self, session):
        with self._lock:
            self.sessions.discard(session)

    def _bt_connection(self, device_path, fd):
        peer = self.bt.device_label(device_path) if self.bt else device_path
        try:
            sock = socket.socket(fileno=fd)
        except OSError as e:
            log.error("cannot wrap RFCOMM fd: %s", e)
            os.close(fd)
            return
        sock.setblocking(True)
        self._add_session(sock, peer, local=False, device_path=device_path)

    def _bt_disconnect(self, device_path):
        with self._lock:
            victims = [s for s in self.sessions if getattr(s, "device_path", None) == device_path]
        for s in victims:
            threading.Thread(target=s.close, args=("device disconnected",), daemon=True).start()

    # ---------------------------------------------------------- admin/status
    def status(self):
        bt = self.call_in_main(self.bt.adapter_status) if self.bt else {"ready": False, "disabled": True}
        with self._lock:
            sessions = [s.describe() for s in self.sessions]
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime": int(time.time() - self.started),
            "bluetooth": bt,
            "input": {"backend": self.input.backend, "available": self.input.available},
            "sessions": sessions,
            "config": self.config,
        }

    def open_pairing(self, seconds):
        if not self.bt:
            raise RuntimeError("bluetooth disabled")
        self.call_in_main(self.bt.open_pairing, seconds)

    def remove_device(self, address):
        if not self.bt:
            raise RuntimeError("bluetooth disabled")
        return self.call_in_main(self.bt.remove_device, address)

    # --------------------------------------------------------- control socket
    def _serve_control(self, path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            srv.bind(path)
        finally:
            os.umask(old)
        srv.listen(8)
        log.info("control socket %s", path)
        while True:
            conn, _ = srv.accept()
            self._add_session(conn, "local", local=True)

    # ------------------------------------------------------------------ run
    def _single_instance(self):
        path = os.path.join(runtime_dir(), "arstro-remote.lock")
        self._lock_file = open(path, "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("another arstro-remote daemon is already running")
            sys.exit(2)
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

    def run(self):
        self._single_instance()
        log.info("Arstro Remote %s starting (pid %d, input backend %s, DISPLAY=%s)", __version__,
                 os.getpid(), self.input.backend, os.environ.get("DISPLAY"))
        threading.Thread(target=self._serve_control, args=(control_socket_path(),),
                         name="control", daemon=True).start()

        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib
        from .bluez import Bluetooth

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.loop = GLib.MainLoop()

        def quit_(*_):
            log.info("signal received, stopping")
            self.loop.quit()
            return False

        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, quit_)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, quit_)

        if self.use_bluetooth:
            self.bt = Bluetooth(dbus.SystemBus(), self.config, self._bt_connection, self._bt_disconnect)

            def first_setup():
                try:
                    self.bt.setup()
                except Exception as e:
                    log.error("bluetooth setup failed: %s (retrying in 5s)", e)
                    GLib.timeout_add_seconds(5, first_setup)
                    return False
                mode = self.config.get("pairing", "window")
                if mode == "always":
                    self.bt.open_pairing(-1)
                elif mode == "window":
                    self.bt.open_pairing(int(self.config.get("pair_window_sec", 600)))
                else:
                    self.bt.open_pairing(0)
                return False

            GLib.idle_add(first_setup)
        try:
            self.loop.run()
        finally:
            with self._lock:
                sessions = list(self.sessions)
            for s in sessions:
                s.close("daemon stopping")
            self.terms.close_all()
            if self.bt and self.bt.ready:
                try:
                    self.bt.open_pairing(0)
                except Exception:
                    pass
            try:
                os.unlink(control_socket_path())
            except OSError:
                pass
            log.info("stopped")
