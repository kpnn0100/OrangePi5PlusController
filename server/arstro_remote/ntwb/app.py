"""NTWB app SDK for Python - the reference implementation of the app side (NTWB-09).

    from arstro_remote.ntwb.app import App

    app = App("hello", "1.0.0")

    @app.method("add")
    def add(params, client):
        return params["a"] + params["b"]          # -> result {ok: true, data: ...}

    app.on_client_open = lambda client: app.state("greeting", "hi")
    app.run()                                     # connect to $NTWB_SOCKET, serve until `bye`

A method raising `MethodError("sentence")` answers ok=false with that sentence; any other
exception answers "internal error" and is logged. Everything sent is checked against
`spec` first, so an app built on this SDK cannot emit a message the host would refuse.
"""

import logging
import os
import socket
import threading

from . import spec, wire

log = logging.getLogger("ntwb.app")


class MethodError(Exception):
    """A user-facing failure of a method."""


class App:
    def __init__(self, app_id, version, capabilities=None):
        self.app_id = app_id
        self.version = version
        self.capabilities = list(capabilities or [])
        self.methods = {}
        self.notifications = {}
        self.clients = set()
        self.on_client_open = None
        self.on_client_close = None
        self.on_welcome = None
        self.sock = None
        self.session = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- declaring the API -------------------------------------------------------------------
    def method(self, name):
        def deco(fn):
            self.methods[name] = fn
            return fn
        return deco

    def notification(self, name):
        def deco(fn):
            self.notifications[name] = fn
            return fn
        return deco

    # -- sending ------------------------------------------------------------------------------
    def _send_json(self, msg):
        spec.validate(msg, "a2h")
        data = wire.encode_json(msg)
        with self._lock:
            self.sock.sendall(data)

    def event(self, name, data=None, client=None):
        msg = {"t": "event", "name": name}
        if data is not None:
            msg["data"] = data
        if client:
            msg["client"] = client
        self._send_json(msg)

    def state(self, key, data):
        self._send_json({"t": "state", "key": key, "data": data})

    def blob(self, stream, mime, data, meta=None, client=None, coalesce=False):
        header = {"stream": stream, "mime": mime}
        if meta:
            header["meta"] = meta
        if client:
            header["client"] = client
        if coalesce:
            header["coalesce"] = True
        spec.validate_blob_header(header, "a2h")
        frame = wire.encode_blob(header, data)
        with self._lock:
            self.sock.sendall(frame)

    def log(self, level, msg):
        self._send_json({"t": "log", "level": level, "msg": msg})

    # -- running --------------------------------------------------------------------------------
    def connect(self, path=None, token=None):
        path = path or os.environ.get("NTWB_SOCKET")
        if not path:
            raise RuntimeError("NTWB_SOCKET is not set - start the app from Arstro Remote (Apps)")
        token = token if token is not None else os.environ.get("NTWB_TOKEN")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        hello = {"t": "hello", "ntwb": spec.VERSION, "app": self.app_id, "version": self.version, "pid": os.getpid()}
        if token:
            hello["token"] = token
        if self.capabilities:
            hello["capabilities"] = self.capabilities
        self._send_json(hello)

    def run(self, path=None, token=None):
        """Connect (unless connected) and serve until the host says bye or the socket closes."""
        if self.sock is None:
            self.connect(path, token)
        dec = wire.Decoder()
        while not self._stop.is_set():
            data = self.sock.recv(1 << 20)
            if not data:
                break
            for kind, msg in dec.feed(data):
                if kind != "json":
                    continue
                self._dispatch(msg)
        try:
            self.sock.close()
        except OSError:
            pass

    def stop(self, reason="done"):
        try:
            self._send_json({"t": "bye", "reason": reason})
        except OSError:
            pass
        self._stop.set()

    def _dispatch(self, msg):
        t = msg.get("t")
        if t == "welcome":
            self.session = msg["session"]
            if self.on_welcome:
                self.on_welcome(msg)
        elif t == "client.open":
            self.clients.add(msg["client"])
            if self.on_client_open:
                self.on_client_open(msg["client"])
        elif t == "client.close":
            self.clients.discard(msg["client"])
            if self.on_client_close:
                self.on_client_close(msg["client"])
        elif t == "call":
            fn = self.methods.get(msg["method"])
            reply = {"t": "result", "id": msg["id"]}
            if fn is None:
                reply.update(ok=False, error="no method named %s" % msg["method"])
            else:
                try:
                    data = fn(msg.get("params") or {}, msg.get("client"))
                    reply.update(ok=True)
                    if data is not None:
                        reply["data"] = data
                except MethodError as e:
                    reply.update(ok=False, error=str(e))
                except Exception as e:                      # noqa: BLE001 - reported, not hidden
                    log.exception("method %s crashed", msg["method"])
                    reply.update(ok=False, error="internal error: %s" % e)
            self._send_json(reply)
        elif t == "notify":
            fn = self.notifications.get(msg["method"]) or self.methods.get(msg["method"])
            if fn:
                try:
                    fn(msg.get("params") or {}, msg.get("client"))
                except Exception:                           # noqa: BLE001
                    log.exception("notification %s crashed", msg["method"])
        elif t == "ping":
            self._send_json({"t": "pong", "n": msg["n"]})
        elif t == "bye":
            self._stop.set()
        elif t == "error":
            log.warning("host says: %s (%s)", msg.get("error"), msg.get("about"))
