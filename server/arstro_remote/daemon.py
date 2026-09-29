"""Arstro Remote server: the one process that owns the Pi's hardware and state (ARC-01).

Controllers reach it over
  * Bluetooth RFCOMM (the Android app),
  * the local control socket (the CLI on the Pi),
  * HTTP/WebSocket (the web page, the remote CLI, the app's Wi-Fi media link),
and all of them see the same state through the hub (ARC-03).
"""

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

from . import __version__, modules, wifi
from .hub import Hub
from .paths import (DEFAULT_CONFIG, cache_dir, config_dir, control_socket_path, load_config,  # noqa: F401
                    lock_path, runtime_dir, slot, state_dir)
from .input_x11 import create_input
from .session import Session
from .stats import StatsCollector
from .terminal import TerminalPool

log = logging.getLogger("arstro")

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def setup_logging(debug=False, level=None):
    """Rotating log file per instance (LOG-01): <state>/arstro-remote.log, 5 x 5 MB."""
    os.makedirs(state_dir(), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(threadName)s]: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else LEVELS.get(str(level or "info").lower(), logging.INFO))
    fh = logging.handlers.RotatingFileHandler(os.path.join(state_dir(), "arstro-remote.log"),
                                              maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr.isatty() or os.environ.get("ARSTRO_LOG_STDERR"):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    from .system import install_exception_logging
    install_exception_logging()


class Daemon:
    def __init__(self, config, use_bluetooth=True):
        self.config = config
        self.use_bluetooth = use_bluetooth and bool(config.get("bluetooth_enabled", True))
        self.modules = modules.enabled(config)
        self.module_ok = {}                    # module -> False when it failed to start
        self.exit_code = 0
        self.started = time.time()
        self.hub = Hub()
        self.stats = StatsCollector()
        self.input = create_input()
        self.terms = TerminalPool(int(config.get("term_keep_sec", 600)),
                                  on_change=lambda terms: self.hub.publish("terminals", terms))
        self.sessions = set()
        self._lock = threading.Lock()
        self.bt = None
        self.web = None
        self.recorder = None
        self.screen = None
        self.io = None
        self.files = None
        self.conn = None
        self.system = None
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
    def add_session(self, sock, peer, kind, device_path=None):
        session = Session(sock, self, peer, kind=kind)
        session.device_path = device_path
        with self._lock:
            self.sessions.add(session)
        self.hub.attach(session)
        log.info("session %d started: %s (%s)", session.num, peer, kind)
        session.start()
        self.controllers_changed()
        return session

    def session_closed(self, session):
        with self._lock:
            self.sessions.discard(session)
        self.hub.detach(session)
        self.controllers_changed()

    def sessions_of(self, kinds):
        with self._lock:
            return [s for s in self.sessions if s.kind in kinds]

    def controllers_changed(self):
        with self._lock:
            ctl = [{"session": s.num, "controller": s.controller, "kind": s.kind, "peer": s.peer,
                    "since": int(s.connected_at)} for s in self.sessions if s.client]
        self.hub.publish("controllers", sorted(ctl, key=lambda c: c["session"]))

    def _bt_connection(self, device_path, fd):
        peer = self.bt.device_label(device_path) if self.bt else device_path
        try:
            sock = socket.socket(fileno=fd)
        except OSError as e:
            log.error("cannot wrap RFCOMM fd: %s", e)
            os.close(fd)
            return
        sock.setblocking(True)
        self.add_session(sock, peer, "bluetooth", device_path=device_path)

    def _bt_disconnect(self, device_path):
        with self._lock:
            victims = [s for s in self.sessions if getattr(s, "device_path", None) == device_path]
        for s in victims:
            threading.Thread(target=s.close, args=("device disconnected",), daemon=True).start()

    # ---------------------------------------------------------- admin/status
    def bt_status(self):
        if not self.bt:
            return {"ready": False, "disabled": True}
        try:
            return self.call_in_main(self.bt.adapter_status)
        except Exception as e:
            return {"ready": False, "error": str(e)}

    def status(self):
        with self._lock:
            sessions = [s.describe() for s in self.sessions]
        return {
            "version": __version__,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "uptime": int(time.time() - self.started),
            "bluetooth": self.bt_status(),
            "slot": slot(),
            "modules": self.modules,
            "input": {"backend": self.input.backend, "available": self.input.available},
            "web": self.web.info(include_token=False) if self.web else {"enabled": False},
            "recorder": {"enabled": self.recorder is not None},
            "sessions": sessions,
            "config": {k: v for k, v in self.config.items()},
        }

    def publish_pairing(self):
        st = self.bt_status()
        self.hub.publish("pairing", {
            "ready": st.get("ready", False), "alias": st.get("alias"), "address": st.get("address"),
            "pairing_open": st.get("pairing_open", False),
            "pairing_remaining": st.get("pairing_remaining", 0),
            "paired_devices": st.get("paired_devices", []),
        })

    def open_pairing(self, seconds):
        if not self.bt:
            raise RuntimeError("bluetooth disabled")
        self.call_in_main(self.bt.open_pairing, seconds)
        self.publish_pairing()

    def remove_device(self, address):
        if not self.bt:
            raise RuntimeError("bluetooth disabled")
        r = self.call_in_main(self.bt.remove_device, address)
        self.publish_pairing()
        return r

    def wifi_refresh(self):
        """Read Wi-Fi status now and push it if it changed (WIFI-07)."""
        st = wifi.status()
        self.hub.publish("wifi", st)
        return st

    def _wifi_poller(self):
        interval = max(3, int(self.config.get("wifi_poll_sec", 10)))
        n = 0
        while True:
            if "connection" in self.modules:
                try:
                    self.wifi_refresh()
                except Exception as e:
                    log.debug("wifi poll: %s", e)
                if self.conn and n % 3 == 0:
                    try:
                        self.conn.refresh()
                    except Exception as e:
                        log.debug("net poll: %s", e)
            n += 1
            if self.bt:
                bt = self.hub.get("pairing") or {}
                if bt.get("pairing_open") or not bt:
                    self.publish_pairing()
            time.sleep(interval)

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
            self.add_session(conn, "local", "local")

    # ------------------------------------------------------------------ run
    def _single_instance(self):
        path = lock_path()
        self._lock_file = open(path, "a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("another %s daemon is already running (%s)", "slot " + slot() if slot() else "arstro-remote", path)
            sys.exit(2)
        self._lock_file.seek(0)
        self._lock_file.truncate()
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

    def request_stop(self, exit_code=0):
        """Stop the main loop from any thread (restart: the launcher starts us again)."""
        self.exit_code = exit_code
        if self.loop is None:
            os._exit(exit_code)
        from gi.repository import GLib
        GLib.idle_add(lambda: self.loop.quit() or False)

    def _start(self, module, attr, factory, what):
        if module not in self.modules:
            return
        try:
            svc = factory(self)
            setattr(self, attr, svc)
            svc.start()
            self.module_ok[module] = self.module_ok.get(module, True)
        except Exception:
            log.exception("%s unavailable", what)
            setattr(self, attr, None)
            self.module_ok[module] = False

    def _start_services(self):
        def recorder(ctx):
            from .recorder.service import RecorderService
            return RecorderService(ctx)

        def screen(ctx):
            from .screen.service import ScreenService
            return ScreenService(ctx)

        def io(ctx):
            from .hwio.service import IoService
            return IoService(ctx)

        def files(ctx):
            from .files import FilesService
            return FilesService(ctx)

        def conn(ctx):
            from .netconf import ConnectionService
            return ConnectionService(ctx)

        def system(ctx):
            from .system import SystemService
            return SystemService(ctx)

        self._start("system", "system", system, "system module")
        self._start("camera", "recorder", recorder, "camera (recorder)")
        self._start("screen", "screen", screen, "remote screen")
        self._start("io", "io", io, "IO control")
        self._start("files", "files", files, "files")
        self._start("connection", "conn", conn, "connection")
        if self.config.get("web_enabled", True):
            try:
                from .web.server import WebServer
                self.web = WebServer(self)
                self.web.start()
            except Exception:
                log.exception("web server unavailable")
                self.web = None
        log.info("modules: %s%s", ", ".join(self.modules),
                 " (failed: %s)" % ", ".join(m for m, ok in self.module_ok.items() if not ok)
                 if not all(self.module_ok.values()) else "")

    def run(self):
        self._single_instance()
        log.info("Arstro Remote %s starting: slot %s, pid %d, port %s, bluetooth %s, input %s, DISPLAY=%s",
                 __version__, slot() or "-", os.getpid(), self.config.get("web_port"),
                 "on" if self.use_bluetooth else "off", self.input.backend, os.environ.get("DISPLAY"))

        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.loop = GLib.MainLoop()

        def quit_(*_):
            log.info("signal received, stopping")
            self.loop.quit()
            return False

        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, quit_)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, quit_)

        self.hub.publish("terminals", [])
        self.hub.publish("controllers", [])
        threading.Thread(target=self._serve_control, args=(control_socket_path(),),
                         name="control", daemon=True).start()
        self._start_services()
        threading.Thread(target=self._wifi_poller, name="wifi-poll", daemon=True).start()

        if self.use_bluetooth:
            from .bluez import Bluetooth
            self.bt = Bluetooth(dbus.SystemBus(), self.config, self._bt_connection, self._bt_disconnect)
            self.bt.on_pairing_change = lambda: threading.Thread(
                target=self.publish_pairing, daemon=True).start()

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
                threading.Thread(target=self.publish_pairing, daemon=True).start()
                return False

            GLib.idle_add(first_setup)
        try:
            self.loop.run()
        finally:
            with self._lock:
                sessions = list(self.sessions)
            for s in sessions:
                s.close("server stopping")
            for name in ("recorder", "screen", "io", "files", "conn", "system"):
                svc = getattr(self, name)
                if svc:
                    try:
                        svc.shutdown()
                    except Exception:
                        log.exception("%s shutdown", name)
            if self.web:
                self.web.stop()
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
            log.info("stopped (exit code %d)", self.exit_code)
        return self.exit_code
