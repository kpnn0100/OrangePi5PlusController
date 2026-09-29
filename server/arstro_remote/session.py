"""One controller connection: Bluetooth RFCOMM (app), local socket (CLI on the Pi),
WebSocket (web page, remote CLI, the app's Wi-Fi link) - or a REST call.

All of them run the same ops (`dispatch`), so the three controllers stay feature-equal
(ARC-02/04), and all of them receive the hub's state events (ARC-03).
"""

import json
import logging
import os
import pwd
import select
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import __version__, modules, wifi
from .input_x11 import InputUnavailable
from .protocol import (PROTO_VERSION, T_JSON, T_TERM, FrameDecoder, ProtocolError,
                       encode_json, encode_term_out)

log = logging.getLogger("arstro.session")

FEATURES = ["stats", "wifi", "terminal", "input", "recorder", "gallery", "sync", "web", "screen"]
# hello "features" (what older controllers look at) -> the module that provides it
FEATURE_MODULE = {"stats": "monitor", "wifi": "connection", "terminal": "terminal", "input": "screen",
                  "recorder": "camera", "gallery": "camera", "screen": "screen"}

# Ops that may block for seconds run on a worker pool so input and terminal traffic
# from the same controller never waits behind them.
SLOW_PREFIXES = ("wifi.", "stats.get", "recorder.", "gallery.", "jobs.", "admin.", "web.", "screen.",
                 "camera.", "net.", "bt.", "io.", "files.", "system.", "log.")
# prefix -> daemon attribute of the service that runs those ops
ROUTES = (("recorder.", "recorder"), ("gallery.", "recorder"), ("jobs.", "recorder"), ("camera.", "recorder"),
          ("web.", "web"), ("screen.", "screen"), ("io.", "io"), ("files.", "files"), ("net.", "conn"),
          ("bt.", "conn"), ("system.", "system"), ("log.", "system"))
QUIET_OPS = ("ping", "stats.get", "state.get", "log.tail", "term.resize", "io.gpio.get")
RESPONDED = object()  # handler already sent its own response

# Input ops run on one dedicated thread so their order is preserved (typed text must
# land before the Enter that follows it) without blocking the reader.
INPUT_PREFIX = "in."

APP_TO_CONTROLLER = {"arstro-android": "app", "arstro-web": "web", "arstro-cli": "cli"}
KIND_TO_CONTROLLER = {"bluetooth": "app", "web": "web", "local": "cli", "remote": "cli", "rest": "rest"}


class OpError(Exception):
    """A user-facing error of an op (bad arguments, not possible now...)."""


class Session:
    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, sock, ctx, peer, kind="local"):
        with Session._counter_lock:
            Session._counter += 1
            self.num = Session._counter
        self.sock = sock
        self.ctx = ctx
        self.peer = peer
        self.kind = kind
        self.controller = KIND_TO_CONTROLLER.get(kind, kind)
        self.connected_at = time.time()
        self.client = {}
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._stats_interval = None
        self._stats_wakeup = threading.Event()
        self._state_pending = {}
        self._state_lock = threading.Lock()
        self._state_wakeup = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="s%d-op" % self.num)
        self._input_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s%d-in" % self.num)
        self.bytes_in = 0
        self.bytes_out = 0

    @property
    def local(self):
        return self.kind == "local"

    # --------------------------------------------------------------- lifecycle
    def start(self):
        threading.Thread(target=self._reader, name="s%d-rx" % self.num, daemon=True).start()
        threading.Thread(target=self._stats_loop, name="s%d-stats" % self.num, daemon=True).start()
        threading.Thread(target=self._state_loop, name="s%d-state" % self.num, daemon=True).start()

    @property
    def closed(self):
        return self._closed.is_set()

    def close(self, reason=""):
        if self._closed.is_set():
            return
        self._closed.set()
        self._stats_wakeup.set()
        self._state_wakeup.set()
        log.info("session %d (%s %s) closing %s", self.num, self.controller, self.peer, reason)
        try:
            self.ctx.terms.detach_session(self)
        except Exception:
            log.exception("detaching terminals")
        try:
            self.ctx.input.release_all()
        except Exception:
            pass
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._input_pool.shutdown(wait=False, cancel_futures=True)
        self.ctx.session_closed(self)

    # -------------------------------------------------------------------- send
    SEND_STALL_LIMIT = 30.0     # a link that accepts nothing for this long is dead

    def _sendall(self, data):
        """sendall() that survives EAGAIN: RFCOMM sockets can report it on a slow link
        even in blocking mode, and plain sendall() then loses track of what was sent."""
        send = getattr(self.sock, "send", None)
        if send is None:                     # WebSocket stream: its own sendall
            self.sock.sendall(data)
            return
        view = memoryview(data)
        stalled_since = None
        while view:
            try:
                n = send(view)
            except (BlockingIOError, InterruptedError):
                n = 0
            if n:
                view = view[n:]
                stalled_since = None
                continue
            now = time.monotonic()
            stalled_since = stalled_since or now
            if now - stalled_since > self.SEND_STALL_LIMIT:
                raise OSError("link stalled for %ds" % self.SEND_STALL_LIMIT)
            select.select([], [self.sock], [], 1.0)

    def _send(self, data: bytes):
        if self._closed.is_set() or self.sock is None:
            raise ConnectionError("session closed")
        with self._send_lock:
            try:
                self._sendall(data)
                self.bytes_out += len(data)
            except OSError as e:
                threading.Thread(target=self.close, args=("send failed: %s" % e,), daemon=True).start()
                raise ConnectionError(str(e))

    def send_json(self, obj):
        self._send(encode_json(obj))

    def send_term(self, term_id, data, offset):
        """Shell output; `offset` is the stream position of data[0]."""
        # Large bursts are split so a JSON reply is never stuck behind megabytes.
        for i in range(0, len(data), 16384):
            self._send(encode_term_out(term_id, offset + i, data[i:i + 16384]))

    def event(self, name, **fields):
        fields["ev"] = name
        try:
            self.send_json(fields)
        except ConnectionError:
            pass

    # ------------------------------------------------------------ state events
    def post_state(self, topic, data):
        """Queue a state event (called by the hub from any thread). Several updates of
        one topic before the sender runs collapse into the newest one."""
        if self._closed.is_set() or self.sock is None:
            return
        with self._state_lock:
            self._state_pending[topic] = data
        self._state_wakeup.set()

    def _state_loop(self):
        while not self._closed.is_set():
            self._state_wakeup.wait()
            self._state_wakeup.clear()
            with self._state_lock:
                pending, self._state_pending = self._state_pending, {}
            for topic, data in pending.items():
                try:
                    self.send_json({"ev": "state", "topic": topic, "data": data})
                except ConnectionError:
                    return

    # --------------------------------------------------------------------- rx
    def _reader(self):
        decoder = FrameDecoder()
        reason = "eof"
        try:
            while not self._closed.is_set():
                data = self.sock.recv(65536)
                if not data:
                    break
                self.bytes_in += len(data)
                for ftype, payload in decoder.feed(data):
                    if ftype == T_TERM:
                        if payload:
                            self.ctx.terms.write(self, payload[0], payload[1:])
                    elif ftype == T_JSON:
                        self._on_json(payload)
        except ProtocolError as e:
            reason = "protocol error: %s" % e
        except OSError as e:
            reason = "recv: %s" % e
        except Exception as e:
            log.exception("reader crashed")
            reason = "error: %s" % e
        self.close(reason)

    def _on_json(self, payload):
        try:
            msg = json.loads(payload.decode("utf-8"))
            op = msg["op"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            log.warning("bad json message: %r", payload[:200])
            return
        if op.startswith(INPUT_PREFIX):
            pool = self._input_pool
        elif op.startswith(SLOW_PREFIXES):
            pool = self._pool
        else:
            pool = None
        if pool is None:
            self._handle(msg)
            return
        try:
            pool.submit(self._handle, msg)
        except RuntimeError:
            pass  # pool shut down

    def _handle(self, msg):
        op = msg.get("op")
        req_id = msg.get("id")
        t0 = time.monotonic()
        try:
            result = self.dispatch(msg)
            if req_id is not None and result is not RESPONDED:
                self.send_json({"id": req_id, "ok": True, "data": result})
            if not op.startswith(INPUT_PREFIX) and op not in QUIET_OPS and log.isEnabledFor(logging.DEBUG):
                log.debug("op %s by session %d (%s) ok in %.0f ms", op, self.num, self.controller,
                          (time.monotonic() - t0) * 1000)
        except ConnectionError:
            pass
        except (OpError, wifi.WifiError, InputUnavailable, ValueError, KeyError, RuntimeError,
                OSError, TypeError) as e:
            if not isinstance(e, InputUnavailable) or req_id is not None:
                log.warning("op %s by session %d (%s) failed: %s: %s", op, self.num, self.controller,
                            type(e).__name__, e)
                log.debug("op %s traceback", op, exc_info=True)
            if req_id is not None:
                try:
                    self.send_json({"id": req_id, "ok": False, "error": str(e).strip("'\"")})
                except ConnectionError:
                    pass
        except Exception as e:
            log.exception("op %s crashed", op)
            if req_id is not None:
                try:
                    self.send_json({"id": req_id, "ok": False, "error": "internal error: %s" % e})
                except ConnectionError:
                    pass

    def dispatch(self, msg):
        """Run one op and return its result (raises on error)."""
        op = msg.get("op") or ""
        mod = modules.module_of(op)
        enabled = getattr(self.ctx, "modules", None)
        # serial consoles (IO module) are terminals too: attach/resize/... stay usable
        serial_ok = mod == "terminal" and op != "term.open" and enabled is not None and "io" in enabled
        if mod and enabled is not None and mod not in enabled and not serial_ok:
            raise OpError("the %s module is not enabled on this server (op %s)" % (modules.MODULES[mod][0], op))
        for prefix, service in ROUTES:
            if op.startswith(prefix):
                svc = getattr(self.ctx, service, None)
                if svc is None:
                    raise OpError("%s is not available on this server" % prefix.rstrip("."))
                return svc.handle(self, op, msg)
        handler = getattr(self, "op_" + op.replace(".", "_"), None)
        if handler is None:
            raise OpError("unknown op %s" % op)
        return handler(msg)

    # ------------------------------------------------------------------ stats
    def _stats_loop(self):
        while not self._closed.is_set():
            interval = self._stats_interval
            if interval is None:
                self._stats_wakeup.wait()
                self._stats_wakeup.clear()
                continue
            try:
                self.send_json({"ev": "stats", "data": self.ctx.stats.collect()})
            except ConnectionError:
                return
            except Exception:
                log.exception("stats collection failed")
            self._stats_wakeup.wait(interval)
            self._stats_wakeup.clear()

    # ------------------------------------------------------------ general ops
    def op_hello(self, msg):
        self.client = {"app": msg.get("app"), "version": msg.get("version"),
                       "device": msg.get("device"), "client_id": msg.get("client_id")}
        self.controller = APP_TO_CONTROLLER.get(msg.get("app"), self.controller)
        log.info("session %d hello from %s (%s)", self.num, self.client, self.controller)
        self.ctx.controllers_changed()
        enabled = getattr(self.ctx, "modules", list(modules.MODULES))
        from .paths import slot
        return {
            "name": "Arstro Remote",
            "version": __version__,
            "proto": PROTO_VERSION,
            "hostname": socket.gethostname(),
            "user": pwd.getpwuid(os.getuid()).pw_name,
            "features": [f for f in FEATURES if FEATURE_MODULE.get(f, "system") in enabled],
            "modules": modules.describe(enabled, getattr(self.ctx, "module_ok", None)),
            "slot": slot(),
            "session": self.num,
            "controller": self.controller,
            "input": {"backend": self.ctx.input.backend, "available": self.ctx.input.available},
            "terminals": self.ctx.terms.list(),
            "term_keep_sec": self.ctx.terms.keep_sec,
            "state": self.ctx.hub.snapshot(),
        }

    def op_ping(self, msg):
        return {"t": time.time(), "echo": msg.get("t")}

    def op_state_get(self, msg):
        return self.ctx.hub.snapshot(msg.get("topics"))

    def op_stats_get(self, _msg):
        return self.ctx.stats.collect()

    def op_stats_subscribe(self, msg):
        interval = max(0.5, min(float(msg.get("interval_ms", 2000)) / 1000.0, 60.0))
        self._stats_interval = interval
        self._stats_wakeup.set()
        return {"interval_ms": int(interval * 1000)}

    def op_stats_unsubscribe(self, _msg):
        self._stats_interval = None
        return {}

    # --------------------------------------------------------------- wifi ops
    def op_wifi_status(self, _msg):
        return self.ctx.wifi_refresh()

    def op_wifi_scan(self, msg):
        return wifi.scan(rescan=msg.get("rescan", True))

    def op_wifi_saved(self, _msg):
        return {"networks": wifi.saved_networks()}

    def op_wifi_connect(self, msg):
        log.info("wifi connect to %r (password: %s, hidden: %s) from %s", msg.get("ssid"),
                 "yes" if msg.get("password") else "no", bool(msg.get("hidden")), self.controller)
        try:
            return wifi.connect(msg.get("ssid"), msg.get("password") or None, bool(msg.get("hidden")))
        finally:
            self.ctx.wifi_refresh()

    def op_wifi_disconnect(self, _msg):
        try:
            return wifi.disconnect()
        finally:
            self.ctx.wifi_refresh()

    def op_wifi_forget(self, msg):
        target = msg.get("uuid") or msg.get("name")
        if target and not msg.get("uuid"):
            match = [n for n in wifi.saved_networks() if target in (n["name"], n["ssid"])]
            if not match:
                raise OpError("no saved network %r" % target)
            target = match[0]["uuid"]
        try:
            return wifi.forget(target)
        finally:
            self.ctx.wifi_refresh()

    def op_wifi_radio(self, msg):
        try:
            return wifi.set_radio(bool(msg.get("enabled", True)))
        finally:
            self.ctx.wifi_refresh()

    # ----------------------------------------------------------- terminal ops
    def op_term_open(self, msg):
        term_id = self.ctx.terms.open(self, int(msg.get("cols", 80)), int(msg.get("rows", 24)),
                                      ephemeral=bool(msg.get("ephemeral")))
        return {"term": term_id}

    def op_term_list(self, _msg):
        return {"terminals": self.ctx.terms.list()}

    def op_term_attach(self, msg):
        req_id = msg.get("id")

        def respond(data):
            if req_id is not None:
                self.send_json({"id": req_id, "ok": True, "data": data})

        self.ctx.terms.attach(self, int(msg["term"]), msg.get("cols"), msg.get("rows"),
                              since=msg.get("since"), respond=respond)
        return RESPONDED

    def op_term_detach(self, msg):
        self.ctx.terms.detach(self, int(msg["term"]))
        return {}

    def op_term_resize(self, msg):
        self.ctx.terms.resize(self, int(msg["term"]), int(msg["cols"]), int(msg["rows"]))
        return {}

    def op_term_close(self, msg):
        self.ctx.terms.close(self, int(msg["term"]))
        return {}

    def op_term_input(self, msg):
        """JSON alternative to TERM frames (handy for scripts and REST)."""
        term = int(msg["term"])
        t = self.ctx.terms._get(term)
        t.write(msg["data"].encode("utf-8"))
        return {}

    # -------------------------------------------------------------- input ops
    def op_in_move(self, msg):
        self.ctx.input.move(msg.get("dx", 0), msg.get("dy", 0))

    def op_in_btn(self, msg):
        self.ctx.input.button(msg.get("b", "left"), msg.get("a", "click"))

    def op_in_scroll(self, msg):
        self.ctx.input.scroll(msg.get("dx", 0), msg.get("dy", 0))

    def op_in_key(self, msg):
        self.ctx.input.key(msg["k"], msg.get("mods") or (), msg.get("a", "press"))

    def op_in_text(self, msg):
        self.ctx.input.text(msg.get("s", ""))

    def op_in_pointer(self, _msg):
        return self.ctx.input.pointer()

    def op_in_move_to(self, msg):
        self.ctx.input.move_to(msg["x"], msg["y"])
        return {}

    # -------------------------------------------------------------- admin ops
    # Every session is authenticated (trusted Bluetooth device, token, or the
    # user's own 0600 socket), so admin is available to all controllers (ARC-04).
    def op_admin_status(self, _msg):
        return self.ctx.status()

    def op_admin_pair(self, msg):
        seconds = int(msg.get("seconds", self.ctx.config["pair_window_sec"]))
        self.ctx.open_pairing(seconds)
        return self.ctx.status()

    def op_admin_unpair(self, msg):
        return self.ctx.remove_device(msg["address"])

    def describe(self):
        return {
            "num": self.num,
            "kind": self.kind,
            "controller": self.controller,
            "peer": self.peer,
            "local": self.local,
            "client": self.client,
            "connected_for": int(time.time() - self.connected_at),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "stats_subscribed": self._stats_interval is not None,
        }


class RestSession(Session):
    """A single REST call (`POST /api/op/<op>`): same ops, no socket, no events."""

    def __init__(self, ctx, peer):
        super().__init__(None, ctx, peer, kind="rest")

    def start(self):
        pass

    def close(self, reason=""):
        self._closed.set()
        self._pool.shutdown(wait=False)
        self._input_pool.shutdown(wait=False)
