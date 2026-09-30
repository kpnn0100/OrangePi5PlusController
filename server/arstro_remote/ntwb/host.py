"""AppsService - the NTWB host inside Arstro Remote (APP-01..08, NTWB-03..09).

    browser --WS /ws/app/<id>--> WebClient ─┐                 ┌─ AppConn <--unix socket-- app process
    CLI / agent --apps.call-->  OpClient  ──┼── AppInstance ──┤    (launched by us, or attached)
                                             │  retained state │
                                             └─ pending calls ─┘

One `AppInstance` per app id (manifests say `single`), shared by every client. The host
never interprets an app's methods: it validates every message against `spec`, rewrites
call ids so clients cannot collide, routes results/events/blobs, retains `state`, and
supervises the process (hello timeout, ping, orderly stop, exit status).

Publishes the hub topic "apps" (the list with each app's run state and client count).
"""

import collections
import itertools
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import threading
import time

from .. import __version__
from ..paths import instance_name, runtime_dir, save_config_values, state_dir
from ..session import OpError
from . import registry, spec, wire

log = logging.getLogger("arstro.apps")

CLIENT_QUEUE_MAX = 2000          # JSON messages queued for one slow client before it is dropped


def socket_path():
    return os.path.join(runtime_dir(), instance_name() + "-ntwb.sock")


def app_log_path(app_id):
    return os.path.join(state_dir(), "app-%s.log" % app_id)


# ------------------------------------------------------------------------------ clients
class Client:
    """Somebody attached to an app. Messages to it go through a queue drained by its own
    writer thread, so a slow browser never stalls the app's socket reader; queued blobs of a
    live stream (`coalesce`) are replaced by newer ones (a preview), all others are delivered."""

    _ids = itertools.count(1)

    def __init__(self, peer, controller):
        self.id = "c%d" % next(Client._ids)
        self.peer = peer
        self.controller = controller
        self._q = collections.deque()
        self._cv = threading.Condition()
        self.closed = False

    def info(self):
        return {"peer": self.peer, "controller": self.controller}

    def put_json(self, msg):
        with self._cv:
            if self.closed:
                return
            if len(self._q) > CLIENT_QUEUE_MAX:
                self.closed = True
                self._cv.notify_all()
                log.warning("client %s too slow (%d queued) - dropped", self.id, len(self._q))
                return
            self._q.append(("json", msg))
            self._cv.notify()

    def put_blob(self, header, data):
        with self._cv:
            if self.closed:
                return
            if header.get("coalesce"):             # a live stream: a newer frame replaces an unsent one
                stream = header.get("stream")
                for i, item in enumerate(self._q):
                    if item[0] == "blob" and item[1][0].get("stream") == stream and item[1][0].get("coalesce"):
                        self._q[i] = ("blob", (header, data))
                        return
            self._q.append(("blob", (header, data)))
            self._cv.notify()

    def next_item(self, timeout=1.0):
        with self._cv:
            if not self._q and not self.closed:
                self._cv.wait(timeout)
            if self._q:
                return self._q.popleft()
            return None

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()


class WebClient(Client):
    """A browser on /ws/app/<id>."""

    def __init__(self, conn, peer):
        super().__init__(peer, "web")
        self.conn = conn
        threading.Thread(target=self._writer, name="ntwb-%s-tx" % self.id, daemon=True).start()

    def _writer(self):
        from ..web import ws
        try:
            while not self.closed:
                item = self.next_item()
                if item is None:
                    continue
                kind, body = item
                if kind == "json":
                    self.conn.send_text(_dumps(body))
                else:
                    self.conn.send_binary(wire.blob_payload(*body))
        except (OSError, ws.WSClosed):
            pass
        self.close()
        try:
            self.conn.close()
        except OSError:
            pass


class OpClient(Client):
    """`apps.call` from a protocol session (CLI, app, agent): one call, answered in place."""

    def __init__(self, session):
        super().__init__("session %d" % session.num, session.controller)
        self.result = None
        self.done = threading.Event()

    def put_json(self, msg):
        if msg.get("t") == "result":
            self.result = msg
            self.done.set()
        elif msg.get("t") == "status" and msg.get("state") in ("stopped", "failed"):
            self.result = {"ok": False, "error": "the app %s: %s" % (msg["state"], msg.get("detail") or "")}
            self.done.set()

    def put_blob(self, header, data):
        pass


def _dumps(msg):
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------------------------ the app side
class AppConn:
    """The socket to one app process (after `hello`)."""

    def __init__(self, sock):
        self.sock = sock
        self._lock = threading.Lock()
        self.closed = False
        self.last_rx = time.monotonic()

    def send(self, msg):
        data = wire.encode_json(msg)
        with self._lock:
            if self.closed:
                return False
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                self.close()
                return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except OSError:
                pass


class AppInstance:
    """The running (or starting / stopped) instance of one app id."""

    def __init__(self, svc, app):
        self.svc = svc
        self.app = app
        self.id = app.id
        self.proc = None
        self.token = None
        self.conn = None
        self.session_id = None
        self.state = "stopped"
        self.detail = None
        self.started = None
        self.app_version = None
        self.clients = {}             # id -> Client
        self.retained = {}            # state key -> data
        self.pending = {}             # host call id -> (client, client call id)
        self._ids = itertools.count(1)
        self.lock = threading.RLock()
        self.stopping = False

    # -- clients -------------------------------------------------------------------------
    def attach(self, client):
        with self.lock:
            self.clients[client.id] = client
            client.put_json({"t": "status", "state": self.state, **({"detail": self.detail} if self.detail else {})})
            if self.conn:
                client.put_json(self._ready(client))
                self.conn.send({"t": "client.open", "client": client.id, "info": client.info()})
        self.svc.publish()

    def detach(self, client):
        with self.lock:
            if self.clients.pop(client.id, None) is None:
                return
            for hid, (c, _cid) in list(self.pending.items()):
                if c is client:
                    del self.pending[hid]
            if self.conn:
                self.conn.send({"t": "client.close", "client": client.id})
        client.close()
        self.svc.publish()

    def _ready(self, client):
        return {"t": "ready", "ntwb": spec.VERSION, "client": client.id,
                "app": {"id": self.id, "name": self.app.m["name"], "version": self.app_version or self.app.m["version"]},
                "state": dict(self.retained)}

    def broadcast_status(self):
        msg = {"t": "status", "state": self.state}
        if self.detail:
            msg["detail"] = self.detail
        for c in list(self.clients.values()):
            c.put_json(msg)
        self.svc.publish()

    # -- client -> app ---------------------------------------------------------------------
    def from_client(self, client, msg):
        """A validated c2h message. Returns an error string or None."""
        t = msg["t"]
        method = msg["method"]
        if not self.app.allows(method):
            return "the app %s has no method named %s" % (self.id, method)
        with self.lock:
            if not self.conn:
                return "the app %s is %s" % (self.id, self.state)
            out = {"t": t, "method": method, "params": msg.get("params") or {}, "client": client.id}
            if t == "call":
                hid = "h%d" % next(self._ids)
                self.pending[hid] = (client, msg["id"])
                out["id"] = hid
            if not self.conn.send(out):
                return "the app %s is not reachable" % self.id
        return None

    # -- app -> clients --------------------------------------------------------------------
    def from_app(self, msg):
        t = msg["t"]
        if t == "result":
            with self.lock:
                route = self.pending.pop(msg["id"], None)
            if route is None:
                raise spec.SpecError("result for unknown call %r" % msg["id"])
            client, cid = route
            client.put_json(dict(msg, id=cid))
        elif t == "event":
            out = {k: v for k, v in msg.items() if k != "client"}
            self._deliver(msg.get("client"), lambda c: c.put_json(out))
        elif t == "state":
            with self.lock:
                if msg["data"] is None:
                    self.retained.pop(msg["key"], None)
                else:
                    self.retained[msg["key"]] = msg["data"]
            self._deliver(None, lambda c: c.put_json(msg))
        elif t == "log":
            level = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
                     "error": logging.ERROR}[msg["level"]]
            logging.getLogger("arstro.app.%s" % self.id).log(level, "%s", msg["msg"])
            self.svc.app_log_line(self.id, "%s %s" % (msg["level"].upper(), msg["msg"]))
        elif t == "pong":
            pass
        elif t == "bye":
            log.info("app %s says bye: %s", self.id, msg.get("reason") or "")
            self.stopping = True
        else:                                                  # hello twice
            raise spec.SpecError("message %r is not expected now" % t)

    def blob_from_app(self, header, data):
        out = {k: v for k, v in header.items() if k != "client"}
        self._deliver(header.get("client"), lambda c: c.put_blob(out, data))

    def _deliver(self, client_id, fn):
        with self.lock:
            targets = [self.clients.get(client_id)] if client_id else list(self.clients.values())
        for c in targets:
            if c is not None:
                fn(c)

    # -- lifecycle ---------------------------------------------------------------------------
    def connected(self, conn, hello):
        with self.lock:
            self.conn = conn
            self.session_id = secrets.token_hex(6)
            self.app_version = hello.get("version")
            self.state = "running"
            self.detail = None
            conn.send({"t": "welcome", "ntwb": spec.VERSION, "session": self.session_id,
                       "host": {"name": "arstro-remote", "version": __version__}, "clients": list(self.clients)})
            for c in self.clients.values():
                conn.send({"t": "client.open", "client": c.id, "info": c.info()})
        log.info("app %s connected (version %s, pid %s)", self.id, hello.get("version"), hello.get("pid"))
        self.broadcast_status()
        with self.lock:
            for c in self.clients.values():
                c.put_json(self._ready(c))

    def disconnected(self, why):
        with self.lock:
            if self.conn is None:
                return
            self.conn.close()
            self.conn = None
            pending, self.pending = self.pending, {}
            self.retained = {}                  # belonged to that run of the app; a new run republishes
            if self.proc is None:                              # an attached app: it is gone
                self.state, self.detail = "stopped", why
        for hid, (client, cid) in pending.items():
            client.put_json({"t": "result", "id": cid, "ok": False, "error": "the app disconnected: %s" % why})
        log.info("app %s disconnected: %s", self.id, why)
        self.broadcast_status()

    def describe(self):
        d = self.app.describe()
        d.update(state=self.state, detail=self.detail, pid=self.proc.pid if self.proc and self.proc.poll() is None else None,
                 clients=len([c for c in self.clients.values() if isinstance(c, WebClient)]),
                 started=int(self.started) if self.started else None, running_version=self.app_version)
        return d


# ------------------------------------------------------------------------------ the service
class AppsService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.apps = {}                # id -> registry.App (last scan)
        self.instances = {}           # id -> AppInstance
        self.lock = threading.RLock()
        self._listener = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self):
        self.rescan()
        path = socket_path()
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
        srv.listen(16)
        self._listener = srv
        threading.Thread(target=self._accept_loop, name="ntwb-accept", daemon=True).start()
        threading.Thread(target=self._pinger, name="ntwb-ping", daemon=True).start()
        log.info("NTWB %s host on %s, %d app(s) installed", spec.VERSION, path,
                 len([a for a in self.apps.values() if not a.problem]))

    def shutdown(self):
        self._stop.set()
        with self.lock:
            insts = list(self.instances.values())
        for inst in insts:
            try:
                self.stop_app(inst.id, "server stopping", wait=True)
            except Exception:
                log.exception("stopping app %s", inst.id)
        if self._listener:
            try:
                self._listener.close()
                os.unlink(socket_path())
            except OSError:
                pass

    # -- registry -----------------------------------------------------------------------------
    def _registered(self):
        return list(self.ctx.config.get("apps_registered") or [])

    def rescan(self):
        apps = registry.scan(self._registered())
        with self.lock:
            self.apps = apps
            for key, a in apps.items():
                if a.valid and key in self.instances:
                    self.instances[key].app = a
        self.publish()
        return apps

    def publish(self):
        self.ctx.hub.publish("apps", self.list())

    def list(self):
        with self.lock:
            out = []
            for key, a in self.apps.items():
                inst = self.instances.get(key)
                out.append(inst.describe() if inst else dict(a.describe(), state="stopped", detail=None, pid=None,
                                                               clients=0, started=None, running_version=None))
            return sorted(out, key=lambda d: (d["problem"] is not None, str(d["name"]).lower()))

    def get_app(self, app_id):
        with self.lock:
            a = self.apps.get(app_id)
        if a is None:
            a = self.rescan().get(app_id)
        if a is None:
            raise OpError("no app %r is installed" % app_id)
        if a.problem:
            raise OpError("the app %s cannot run: %s" % (app_id, a.problem))
        return a

    def instance(self, app_id):
        with self.lock:
            inst = self.instances.get(app_id)
            if inst is None:
                inst = AppInstance(self, self.get_app(app_id))
                self.instances[app_id] = inst
            return inst

    # -- launching ----------------------------------------------------------------------------
    def launch(self, app_id, who="?"):
        inst = self.instance(app_id)
        with inst.lock:
            if inst.state in ("starting", "running"):
                return inst
            app = inst.app
            inst.token = secrets.token_urlsafe(16)
            inst.stopping = False
            data_dir = os.path.join(state_dir(), "apps", app_id)
            os.makedirs(data_dir, exist_ok=True)
            env = dict(os.environ)
            env.update(app.m.get("env") or {})
            env.update(NTWB_SOCKET=socket_path(), NTWB_TOKEN=inst.token, NTWB_APP_ID=app_id,
                       NTWB_VERSION=spec.VERSION, NTWB_HOST="arstro-remote", NTWB_DATA_DIR=data_dir)
            for k in ("ARSTRO_SLOT", "ARSTRO_SOCKET"):
                env.pop(k, None)
            logf = self._open_app_log(app_id)
            try:
                inst.proc = subprocess.Popen(app.argv, cwd=app.cwd, env=env, stdin=subprocess.DEVNULL,
                                             stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                inst.state, inst.detail, inst.proc = "failed", "could not start: %s" % e, None
                inst.broadcast_status()
                raise OpError("could not start %s: %s" % (app_id, e))
            finally:
                logf.close()
            inst.state, inst.detail, inst.started = "starting", None, time.time()
            proc = inst.proc
        log.info("app %s launched by %s (pid %d): %s", app_id, who, proc.pid, " ".join(app.argv))
        self.app_log_line(app_id, "--- launched by %s (pid %d)" % (who, proc.pid))
        inst.broadcast_status()
        threading.Thread(target=self._watch_proc, args=(inst, proc), name="ntwb-%s-wait" % app_id,
                         daemon=True).start()
        return inst

    def _watch_proc(self, inst, proc):
        deadline = time.monotonic() + spec.TIMINGS["HELLO_TIMEOUT_S"]
        while proc.poll() is None:
            if inst.state == "starting" and time.monotonic() > deadline and inst.proc is proc:
                log.warning("app %s sent no hello within %ds - stopping it", inst.id, spec.TIMINGS["HELLO_TIMEOUT_S"])
                inst.detail = "no hello within %d s" % spec.TIMINGS["HELLO_TIMEOUT_S"]
                self._terminate(proc)
            time.sleep(0.2)
        code = proc.returncode
        with inst.lock:
            if inst.proc is not proc:
                return
            inst.proc = None
            ok = code == 0 or inst.stopping
            timeout_detail = inst.detail if inst.detail and "hello" in inst.detail else None
            inst.state = "stopped" if ok and not timeout_detail else "failed"
            inst.detail = timeout_detail or ("exited with code %d" % code if code >= 0 else "killed by signal %d" % -code)
        self.app_log_line(inst.id, "--- exited (%s)" % inst.detail)
        log.info("app %s %s (%s)", inst.id, inst.state, inst.detail)
        inst.disconnected(inst.detail)
        inst.broadcast_status()

    def _terminate(self, proc):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                return
            try:
                proc.wait(spec.TIMINGS["STOP_GRACE_S"])
                return
            except subprocess.TimeoutExpired:
                continue

    def stop_app(self, app_id, reason="stopped", wait=False, who="?"):
        with self.lock:
            inst = self.instances.get(app_id)
        if inst is None or inst.state in ("stopped", "failed"):
            return
        log.info("app %s stop requested by %s (%s)", app_id, who, reason)
        inst.stopping = True
        with inst.lock:
            proc, conn = inst.proc, inst.conn
        if conn:
            conn.send({"t": "bye", "reason": reason})

        def finish():
            if proc is not None:
                try:
                    proc.wait(spec.TIMINGS["STOP_GRACE_S"])
                except subprocess.TimeoutExpired:
                    self._terminate(proc)
            else:
                inst.disconnected(reason)
        if wait:
            finish()
        else:
            threading.Thread(target=finish, daemon=True).start()

    # -- app log ------------------------------------------------------------------------------
    def _open_app_log(self, app_id):
        p = app_log_path(app_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            if os.path.getsize(p) > 2_000_000:
                os.replace(p, p + ".1")
        except OSError:
            pass
        return open(p, "ab")

    def app_log_line(self, app_id, line):
        try:
            with open(app_log_path(app_id), "a") as f:
                f.write("%s %s\n" % (time.strftime("%F %T"), line))
        except OSError:
            pass

    # -- the app socket -----------------------------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.2)
                continue
            threading.Thread(target=self._serve_app, args=(sock,), name="ntwb-app", daemon=True).start()

    def _serve_app(self, sock):
        conn = AppConn(sock)
        dec = wire.Decoder()
        inst = None
        try:
            sock.settimeout(spec.TIMINGS["HELLO_TIMEOUT_S"])
            items = []
            while not items:                                      # the handshake
                data = sock.recv(65536)
                if not data:
                    return
                items = dec.feed(data)
            kind, hello = items[0]
            if kind != "json":
                raise spec.SpecError("the first message must be hello")
            spec.validate(hello, "a2h")
            if hello["t"] != "hello":
                raise spec.SpecError("the first message must be hello")
            inst = self._accept_hello(hello)
            sock.settimeout(None)
            inst.connected(conn, hello)
            self._handle_items(inst, conn, items[1:])
            while not conn.closed:
                data = sock.recv(1 << 20)
                if not data:
                    break
                conn.last_rx = time.monotonic()
                self._handle_items(inst, conn, dec.feed(data))
            why = "connection closed"
        except spec.SpecError as e:
            conn.send({"t": "error", "error": str(e)})
            log.warning("app connection refused: %s", e)
            why = str(e)
        except wire.WireError as e:
            log.warning("app %s sent a broken frame: %s", inst.id if inst else "?", e)
            why = "broken frame: %s" % e
        except OSError as e:
            why = str(e)
        finally:
            if inst is not None and inst.conn is conn:
                inst.disconnected(why)
            conn.close()

    def _accept_hello(self, hello):
        app_id = hello["app"]
        try:
            inst = self.instance(app_id)
        except OpError as e:
            raise spec.SpecError(str(e))
        with inst.lock:
            if inst.conn is not None:
                raise spec.SpecError("the app %s is already connected" % app_id)
            tok = hello.get("token")
            if tok is not None:
                if tok != inst.token:
                    raise spec.SpecError("wrong token")
            elif "attach" not in (inst.app.m.get("capabilities") or []):
                raise spec.SpecError("the app %s must be launched by the host (no token)" % app_id)
            elif inst.proc is not None:
                raise spec.SpecError("the app %s is being launched by the host" % app_id)
            inst.token = None
        return inst

    def _handle_items(self, inst, conn, items):
        for kind, body in items:
            try:
                if kind == "json":
                    spec.validate(body, "a2h")
                    inst.from_app(body)
                else:
                    header, data = body
                    spec.validate_blob_header(header, "a2h")
                    inst.blob_from_app(header, data)
            except spec.SpecError as e:
                about = body.get("t") if kind == "json" and isinstance(body, dict) else "blob"
                log.warning("app %s: %s", inst.id, e)
                self.app_log_line(inst.id, "PROTOCOL %s" % e)
                conn.send({"t": "error", "error": str(e), "about": str(about)})

    def _pinger(self):
        n = 0
        while not self._stop.wait(spec.TIMINGS["PING_INTERVAL_S"]):
            n += 1
            with self.lock:
                insts = list(self.instances.values())
            for inst in insts:
                conn = inst.conn
                if conn is None:
                    continue
                if time.monotonic() - conn.last_rx > spec.TIMINGS["PONG_TIMEOUT_S"]:
                    log.warning("app %s did not answer ping - dropping its connection", inst.id)
                    conn.close()
                    continue
                conn.send({"t": "ping", "n": n})

    # -- the client side (web) ----------------------------------------------------------------
    def serve_web(self, conn, app_id, peer):
        """Run one browser WebSocket until it closes (called on the HTTP handler thread)."""
        from ..web import ws
        client = WebClient(conn, peer)
        try:
            inst = self.instance(app_id)
        except OpError as e:
            client.put_json({"t": "error", "error": str(e)})
            time.sleep(0.5)
            client.close()
            return
        inst.attach(client)
        log.info("client %s (%s) opened app %s", client.id, peer, app_id)
        if inst.state in ("stopped", "failed"):
            try:
                self.launch(app_id, who="web %s" % peer)
            except OpError as e:
                client.put_json({"t": "error", "error": str(e)})
        try:
            while not client.closed:
                op, data = conn.recv_message()
                if op == ws.OP_BINARY:
                    client.put_json({"t": "error", "error": "clients cannot send blobs"})
                    continue
                msg = None
                try:
                    msg = json.loads(data)
                    spec.validate(msg, "c2h")
                except (ValueError, spec.SpecError) as e:
                    about = msg.get("id") or msg.get("t") if isinstance(msg, dict) else None
                    client.put_json({"t": "error", "error": str(e), **({"about": str(about)} if about else {})})
                    continue
                err = inst.from_client(client, msg)
                if err:
                    if msg["t"] == "call":
                        client.put_json({"t": "result", "id": msg["id"], "ok": False, "error": err})
                    else:
                        client.put_json({"t": "error", "error": err, "about": msg["method"]})
        except (ws.WSClosed, OSError):
            pass
        finally:
            inst.detach(client)
            log.info("client %s left app %s", client.id, app_id)

    # -- ops ------------------------------------------------------------------------------------
    def handle(self, session, op, msg):
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            raise OpError("unknown op %s" % op)
        try:
            return fn(session, msg)
        except spec.SpecError as e:
            raise OpError(str(e))

    def _who(self, session):
        return "session %d (%s)" % (session.num, session.controller)

    def op_apps_list(self, _s, _m):
        self.rescan()
        return {"apps": self.list(), "ntwb": spec.VERSION, "socket": socket_path(),
                "search": registry.search_dirs(), "registered": self._registered()}

    def op_apps_info(self, _s, msg):
        app = self.get_app(msg.get("app"))
        inst = self.instances.get(app.id)
        d = inst.describe() if inst else dict(app.describe(), state="stopped")
        d.update(manifest_data=app.m, web_dir=app.web_dir, argv=app.argv, log=app_log_path(app.id),
                 state_keys=sorted(inst.retained) if inst else [])
        return d

    def op_apps_api(self, _s, msg):
        app = self.get_app(msg.get("app"))
        return app.api or {"ntwb": spec.VERSION, "app": app.id, "methods": {}, "note": "the app ships no API description"}

    def op_apps_launch(self, session, msg):
        inst = self.launch(msg.get("app"), who=self._who(session))
        if msg.get("wait", True):
            end = time.monotonic() + spec.TIMINGS["HELLO_TIMEOUT_S"] + 2
            while inst.state == "starting" and time.monotonic() < end:
                time.sleep(0.05)
        return inst.describe()

    def op_apps_stop(self, session, msg):
        app_id = msg.get("app")
        self.get_app(app_id)
        wait = bool(msg.get("wait", True))
        self.stop_app(app_id, "stopped by %s" % session.controller, wait=wait, who=self._who(session))
        inst = self.instances.get(app_id)
        end = time.monotonic() + 3
        while wait and inst and inst.state in ("starting", "running") and time.monotonic() < end:
            time.sleep(0.05)
        return self.instances[app_id].describe() if app_id in self.instances else {}

    def op_apps_register(self, session, msg):
        path = os.path.abspath(os.path.expanduser(str(msg.get("path") or "")))
        if os.path.isdir(path):
            path = os.path.join(path, "ntwb.json")
        a = registry._load(path, "registered")
        if a.problem and not a.valid:
            raise OpError("not an NTWB manifest: %s" % a.problem)
        reg = self._registered()
        if path not in reg:
            reg.append(path)
            self.ctx.config["apps_registered"] = reg
            save_config_values({"apps_registered": reg})
        log.info("app manifest %s registered by %s", path, self._who(session))
        self.rescan()
        return a.describe()

    def op_apps_unregister(self, session, msg):
        path = os.path.abspath(os.path.expanduser(str(msg.get("path") or "")))
        if os.path.isdir(path):
            path = os.path.join(path, "ntwb.json")
        reg = [p for p in self._registered() if p != path]
        self.ctx.config["apps_registered"] = reg
        save_config_values({"apps_registered": reg})
        log.info("app manifest %s unregistered by %s", path, self._who(session))
        self.rescan()
        return {"registered": reg}

    def op_apps_state(self, _s, msg):
        app = self.get_app(msg.get("app"))
        inst = self.instances.get(app.id)
        state = dict(inst.retained) if inst else {}
        key = msg.get("key")
        if key is not None:
            if key not in state:
                raise OpError("the app %s has no state %r" % (app.id, key))
            return {"key": key, "data": state[key]}
        return {"state": state}

    def op_apps_call(self, session, msg):
        """Call an app method from a protocol session (CLI, scripts, agents) - the same
        path a browser takes, so it is testable without a browser (NTWB-08)."""
        app_id = msg.get("app")
        call = {"t": "call", "id": "op", "method": msg.get("method"), "params": msg.get("params") or {}}
        spec.validate(call, "c2h")
        inst = self.instance(app_id)
        if inst.state in ("stopped", "failed"):
            self.launch(app_id, who=self._who(session))
        end = time.monotonic() + spec.TIMINGS["HELLO_TIMEOUT_S"] + 2
        while inst.state == "starting" and time.monotonic() < end:
            time.sleep(0.05)
        client = OpClient(session)
        with inst.lock:
            inst.clients[client.id] = client
        try:
            err = inst.from_client(client, call)
            if err:
                raise OpError(err)
            if not client.done.wait(float(msg.get("timeout", 60))):
                raise OpError("the app %s did not answer %s within %ss" % (app_id, call["method"], msg.get("timeout", 60)))
            r = client.result
            if not r.get("ok"):
                raise OpError(r.get("error") or "failed")
            return r.get("data")
        finally:
            with inst.lock:
                inst.clients.pop(client.id, None)
                for hid, (c, _cid) in list(inst.pending.items()):
                    if c is client:
                        del inst.pending[hid]

    def op_apps_log(self, _s, msg):
        from ..system import tail
        app = self.get_app(msg.get("app"))
        return {"id": app.id, "path": app_log_path(app.id),
                "lines": tail(app_log_path(app.id), msg.get("lines", 200), msg.get("grep"))}

    # -- HTTP helpers ---------------------------------------------------------------------------
    def web_file(self, app_id, rel):
        """Real path of a file of the app's web UI, or None."""
        with self.lock:
            a = self.apps.get(app_id)
        if a is None:
            a = self.rescan().get(app_id)
        if a is None or not a.valid:
            return None
        if rel in ("", "/"):
            rel = "index.html"
        if rel == "icon" and a.icon_path:
            return a.icon_path if os.path.isfile(a.icon_path) else None
        base = os.path.realpath(a.web_dir)
        full = os.path.realpath(os.path.join(base, rel.lstrip("/")))
        if not (full == base or full.startswith(base + os.sep)) or not os.path.isfile(full):
            return None
        return full
