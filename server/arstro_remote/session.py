"""One client connection (Bluetooth RFCOMM or the local control socket)."""

import json
import logging
import os
import pwd
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import __version__, wifi
from .input_x11 import InputUnavailable
from .protocol import (PROTO_VERSION, T_JSON, T_TERM, FrameDecoder, ProtocolError,
                       encode_json, encode_term_out)

log = logging.getLogger("arstro.session")

FEATURES = ["stats", "wifi", "terminal", "input"]

# Ops that may block for seconds run on a worker pool so input and terminal
# traffic from the same client never waits behind them.
SLOW_OPS = {"wifi.status", "wifi.scan", "wifi.connect", "wifi.disconnect", "wifi.saved",
            "wifi.forget", "wifi.radio", "stats.get"}
RESPONDED = object()  # handler already sent its own response

# Input ops run on one dedicated thread so their order is preserved (typed text
# must land before the Enter that follows it) without blocking the reader.
INPUT_PREFIX = "in."


class Session:
    _counter = 0

    def __init__(self, sock, ctx, peer, local=False):
        Session._counter += 1
        self.num = Session._counter
        self.sock = sock
        self.ctx = ctx
        self.peer = peer
        self.local = local
        self.connected_at = time.time()
        self.client = {}
        # Terminals are keyed by client id; only clients that send a stable
        # client_id in hello get their shells kept across reconnects.
        self.client_id = "session-%d" % self.num
        self.persistent = False
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._stats_interval = None
        self._stats_wakeup = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="s%d-op" % self.num)
        self._input_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s%d-in" % self.num)
        self.bytes_in = 0
        self.bytes_out = 0

    # --------------------------------------------------------------- lifecycle
    def start(self):
        threading.Thread(target=self._reader, name="s%d-rx" % self.num, daemon=True).start()
        threading.Thread(target=self._stats_loop, name="s%d-stats" % self.num, daemon=True).start()

    @property
    def closed(self):
        return self._closed.is_set()

    def close(self, reason=""):
        if self._closed.is_set():
            return
        self._closed.set()
        self._stats_wakeup.set()
        log.info("session %d (%s) closing %s", self.num, self.peer, reason)
        try:
            self.ctx.terms.detach_session(self)
        except Exception:
            log.exception("detaching terminals")
        try:
            self.ctx.input.release_all()
        except Exception:
            pass
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
    def _send(self, data: bytes):
        if self._closed.is_set():
            raise ConnectionError("session closed")
        with self._send_lock:
            try:
                self.sock.sendall(data)
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
        pool = self._input_pool if op.startswith(INPUT_PREFIX) else self._pool if op in SLOW_OPS else None
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
        handler = getattr(self, "op_" + op.replace(".", "_"), None)
        try:
            if handler is None or (op.startswith("admin.") and not self.local):
                raise ValueError("unknown op %s" % op)
            result = handler(msg)
            if req_id is not None and result is not RESPONDED:
                self.send_json({"id": req_id, "ok": True, "data": result})
        except ConnectionError:
            pass
        except (wifi.WifiError, InputUnavailable, ValueError, KeyError, RuntimeError, OSError) as e:
            if not isinstance(e, InputUnavailable) or req_id is not None:
                log.info("op %s failed: %s", op, e)
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
        self.client = {"app": msg.get("app"), "version": msg.get("version"), "device": msg.get("device")}
        if msg.get("client_id"):
            self.client_id = "client-%s" % str(msg["client_id"])[:64]
            self.persistent = True
        log.info("session %d hello from %s", self.num, self.client)
        return {
            "name": "Arstro Remote",
            "version": __version__,
            "proto": PROTO_VERSION,
            "hostname": socket.gethostname(),
            "user": pwd.getpwuid(os.getuid()).pw_name,
            "features": FEATURES,
            "input": {"backend": self.ctx.input.backend, "available": self.ctx.input.available},
            "terminals": self.ctx.terms.list(self),
            "term_keep_sec": self.ctx.terms.keep_sec,
        }

    def op_ping(self, msg):
        return {"t": time.time(), "echo": msg.get("t")}

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
        return wifi.status()

    def op_wifi_scan(self, msg):
        return wifi.scan(rescan=msg.get("rescan", True))

    def op_wifi_saved(self, _msg):
        return {"networks": wifi.saved_networks()}

    def op_wifi_connect(self, msg):
        log.info("wifi connect to %r (password: %s, hidden: %s)", msg.get("ssid"),
                 "yes" if msg.get("password") else "no", bool(msg.get("hidden")))
        return wifi.connect(msg.get("ssid"), msg.get("password") or None, bool(msg.get("hidden")))

    def op_wifi_disconnect(self, _msg):
        return wifi.disconnect()

    def op_wifi_forget(self, msg):
        return wifi.forget(msg.get("uuid"))

    def op_wifi_radio(self, msg):
        return wifi.set_radio(bool(msg.get("enabled", True)))

    # ----------------------------------------------------------- terminal ops
    def op_term_open(self, msg):
        term_id = self.ctx.terms.open(self, int(msg.get("cols", 80)), int(msg.get("rows", 24)))
        return {"term": term_id}

    def op_term_list(self, _msg):
        return {"terminals": self.ctx.terms.list(self)}

    def op_term_attach(self, msg):
        req_id = msg.get("id")

        def respond(data):
            if req_id is not None:
                self.send_json({"id": req_id, "ok": True, "data": data})

        self.ctx.terms.attach(self, int(msg["term"]), msg.get("cols"), msg.get("rows"),
                              since=msg.get("since"), respond=respond)
        return RESPONDED

    def op_term_resize(self, msg):
        self.ctx.terms.resize(self, int(msg["term"]), int(msg["cols"]), int(msg["rows"]))
        return {}

    def op_term_close(self, msg):
        self.ctx.terms.close(self, int(msg["term"]))
        return {}

    def op_term_input(self, msg):
        """JSON alternative to TERM frames (handy for debugging)."""
        self.ctx.terms.write(self, int(msg["term"]), msg["data"].encode("utf-8"))
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

    # -------------------------------------------------- admin (local socket)
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
            "peer": self.peer,
            "local": self.local,
            "client": self.client,
            "connected_for": int(time.time() - self.connected_at),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "stats_subscribed": self._stats_interval is not None,
        }
