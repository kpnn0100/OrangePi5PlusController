"""The device manager: HTTP routes, the device registry and viewer <-> device forwarding.

  GET  B                      -> redirect to B/
  GET  B/                     the device list page (static/index.html)
  GET  B/static/<file>        page assets
  GET  B/sw.js, B/shim.js     the viewer's service worker (Service-Worker-Allowed: B/) and page shim
  GET  B/d/<id>/<port>/...    the boot page (static/boot.html) until the service worker is in control
  GET  B/api/info             {name, version, base, auth, authorized}
  POST B/api/login, B/api/logout    viewer password -> cookie arstro_devices
  GET  B/api/devices          the device list (viewer auth)
  GET  B/api/connect/<id>     WebSocket: a viewer channel (viewer auth)
  GET  B/api/agent            WebSocket: a device's control connection (join token in its hello)
  GET  B/api/agent/data       WebSocket: a device's data connection (device secret)
  GET  B/api/health           {"ok": true}
  GET  B/client/<file>        the standalone client: arstro_relay_client.py, e2e.py, ws.py

Routes, messages and limits: docs/relay/PROTOCOL.md sections 1, 2, 3 and 6. The relay never
decrypts anything: once a viewer and a device's data connection are joined, every message is
forwarded unchanged - only its size is checked and its bytes are counted.
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import signal
import time
import urllib.parse
from collections import deque

from . import __version__
from .store import Store
from .websocket import MAX_HEAD, BadRequest, Closed, accept, read_request, respond, sendable

log = logging.getLogger("relay")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DEFAULT_BASE = "/arstro/internal/devices"
PROTOCOL = 1
MAX_MESSAGE = 1 << 20           # = e2e.MAX_MESSAGE (relay/tests check that they agree)
CHANNELS_PER_DEVICE = 32
MAX_DEVICES = 1000
FIRST_MESSAGE = 10              # s a device has for its first message (hello / data)
IDLE = 60                       # s an idle keep-alive HTTP connection stays open
# Every message name the relay reads or writes = the relay message table of e2e.py (tested).
MESSAGES = ("hello", "welcome", "open", "refuse", "ping", "pong", "bye", "data", "linked", "relay", "error")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
BOOT_RE = re.compile(r"^/d/([^/]+)/([0-9]{1,5})(/.*)?$")
COOKIE = "arstro_devices"
COOKIE_AGE = 30 * 86400
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
         ".webmanifest": "application/manifest+json", ".woff2": "font/woff2",
         ".txt": "text/plain; charset=utf-8", ".py": "text/x-python; charset=utf-8"}
CLIENT_FILES = {"arstro_relay_client.py": "client.py", "e2e.py": "e2e.py", "ws.py": "ws.py"}   # B/client/<name>


class ConfigError(ValueError):
    pass


def _number(env, name, default, kind, lo, hi):
    text = env.get(name)
    if not text:
        return default
    try:
        value = kind(text)
    except ValueError:
        raise ConfigError("%s must be a number, got %r" % (name, text))
    if not lo <= value <= hi:
        raise ConfigError("%s must be between %s and %s, got %s" % (name, lo, hi, text))
    return value


def _flag(env, name):
    value = (env.get(name) or "0").strip().lower()
    if value not in ("0", "1", "false", "true", "no", "yes", "off", "on"):
        raise ConfigError("%s must be 0 or 1, got %r" % (name, value))
    return value in ("1", "true", "yes", "on")


class Config:
    """Settings from the RELAY_* environment variables (relay/README.md has the table)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.host = env.get("RELAY_HOST") or "0.0.0.0"
        self.port = _number(env, "RELAY_PORT", 8090, int, 1, 65535)
        base = env.get("RELAY_BASE", DEFAULT_BASE)
        base = "" if base == "/" else base
        if not re.match(r"^(/[A-Za-z0-9._~-]+)*$", base) or any(s in (".", "..") for s in base.split("/")):
            raise ConfigError("RELAY_BASE must start with / and must not end with / (like %s), got %r"
                              % (DEFAULT_BASE, base))
        self.base = base
        self.data = env.get("RELAY_DATA") or "/data"
        self.join_token = env.get("RELAY_JOIN_TOKEN") or None
        self.password = env.get("RELAY_PASSWORD") or None
        self.trust_proxy = _flag(env, "RELAY_TRUST_PROXY")
        self.name = (env.get("RELAY_NAME") or "Devices")[:80]
        self.static = env.get("RELAY_STATIC") or os.path.join(HERE, "static")
        packaged = os.path.join(HERE, "client")
        self.client_dir = env.get("RELAY_CLIENT_DIR") or (
            packaged if os.path.isdir(packaged) else os.path.join(REPO, "server", "arstro_remote", "relay"))
        self.heartbeat = _number(env, "RELAY_HEARTBEAT", 20, float, 0.2, 3600)
        self.pending = _number(env, "RELAY_PENDING", 10, float, 0.2, 600)
        rate = env.get("RELAY_RATE_LIMIT") or "30/10"
        m = re.match(r"^([1-9][0-9]*)/([0-9]+(\.[0-9]+)?)$", rate)
        if not m or float(m.group(2)) <= 0:
            raise ConfigError("RELAY_RATE_LIMIT must look like 30/10 (connections / seconds), got %r" % rate)
        self.rate = (int(m.group(1)), float(m.group(2)))


def _text(value, n=64):
    """A hello field as short printable text (it ends up in the list and the log)."""
    if value is None:
        return None
    return "".join(c for c in str(value) if c.isprintable())[:n]


def _cookie(header, name):
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def _secret_ok(record, secret):
    return isinstance(secret, str) and hmac.compare_digest(
        hashlib.sha256(secret.encode()).hexdigest(), str(record.get("secret_sha256", "")))


class Refused(Exception):
    """A viewer channel could not be joined (the text goes to the viewer)."""


class Device:
    """An online device: its control connection."""

    def __init__(self, dev_id, name, ws, addr):
        self.id, self.name, self.ws, self.addr = dev_id, name, ws, addr
        self.since = self.heard = time.time()
        self.reason = None          # why the connection ended (for the log)


class Link:
    """A joined viewer channel: the viewer's socket and the device's data socket."""

    def __init__(self, dev_id, viewer, data, addr):
        self.dev_id, self.viewer, self.data, self.addr = dev_id, viewer, data, addr
        self.up = self.down = 0
        self.t0 = time.monotonic()


class Relay:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.base
        self.store = Store(cfg.data)
        self.devices = self.store.devices()
        self._dev_stamp = self.store.stamp(self.store.devices_path)
        self.token = cfg.join_token or self.store.join_token()
        self._token_stamp = self.store.stamp(self.store.token_path)
        key = self.store.cookie_key()
        self.cookie = hmac.new(key, b"arstro-devices viewer " + (cfg.password or "").encode(),
                               hashlib.sha256).hexdigest()
        self.online = {}            # id -> Device
        self.pending = {}           # sid -> (device id, future of (data socket, done event))
        self.links = {}             # id -> {Link}
        self.hits = {}              # client address -> deque of recent connection times
        self._limited = set()
        self._tasks = set()
        self.stop = None

    # ------------------------------------------------------------------ run
    async def run(self):
        loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        server = await asyncio.start_server(self._connection, self.cfg.host, self.cfg.port, limit=MAX_HEAD)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        log.info("device manager %s on http://%s:%d%s/ (data in %s, viewers: %s)", __version__, self.cfg.host,
                 self.cfg.port, self.base, self.cfg.data,
                 "password" if self.cfg.password else "open - set RELAY_PASSWORD to require one")
        if self.cfg.join_token:
            log.info("devices join with the token from RELAY_JOIN_TOKEN")
        else:
            log.info("devices join with token: %s", self.token)
        watcher = asyncio.ensure_future(self._watch())
        await self.stop.wait()
        log.info("shutting down")
        watcher.cancel()
        server.close()
        jobs = [self._kick(d, "the device manager is shutting down") for d in list(self.online.values())]
        for links in list(self.links.values()):
            for link in list(links):
                jobs += [link.viewer.close(1001, "shutting down"), link.data.close(1001, "shutting down")]
        if jobs:
            try:
                await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 5)
            except asyncio.TimeoutError:
                pass

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _watch(self):
        """Pick up the admin CLI's changes (forget, token rotation) and forget old rate-limit entries."""
        while True:
            await asyncio.sleep(1)
            try:
                self._check_files()
            except Exception:
                log.exception("checking the data directory failed")
            now, window = time.monotonic(), self.cfg.rate[1]
            for addr in [a for a, q in self.hits.items() if not q or q[-1] <= now - window]:
                del self.hits[addr]
                self._limited.discard(addr)

    def _check_files(self):
        stamp = self.store.stamp(self.store.devices_path)
        if stamp != self._dev_stamp:
            self._dev_stamp = stamp
            self.devices = self.store.devices()
            for dev in list(self.online.values()):
                if dev.id not in self.devices:
                    self._spawn(self._kick(dev, "this device was removed from the device manager"))
                    for link in list(self.links.get(dev.id, ())):
                        self._spawn(link.viewer.close(1001, "the device was removed"))
                        self._spawn(link.data.close(1001, "the device was removed"))
        stamp = self.store.stamp(self.store.token_path)
        if stamp != self._token_stamp:
            self._token_stamp = stamp
            token = None if self.cfg.join_token else self.store.join_token(create=False)
            if token and token != self.token:
                self.token = token
                log.info("the join token was changed: devices need the new token from now on")

    def _save(self, change):
        try:
            self.devices = self.store.update(change)
            self._dev_stamp = self.store.written
        except OSError as e:
            log.error("cannot write %s: %s", self.store.devices_path, e)
            change(self.devices)

    # ------------------------------------------------------------------ HTTP
    async def _connection(self, reader, writer):
        peer = writer.get_extra_info("peername")
        peer = str(peer[0]) if peer else "?"
        try:
            while True:
                try:
                    req = await read_request(reader, IDLE)
                except BadRequest as e:
                    await self._json(writer, None, e.status, {"error": e.message}, close=True)
                    break
                if req is None:
                    break
                req.addr, req.https = self._client(req, peer)
                if not await self._route(req, reader, writer) or not req.keep_alive:
                    break
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("request failed")
        finally:
            writer.close()

    def _client(self, req, peer):
        """-> (client address, came over https)."""
        if not self.cfg.trust_proxy:
            return peer, False
        addr = req.header("x-forwarded-for").split(",")[0].strip()
        proto = req.header("x-forwarded-proto").split(",")[0].strip().lower()
        return _text(addr or peer), proto == "https"

    async def _route(self, req, reader, writer):
        """Answer one request. -> False when the connection is done (WebSockets)."""
        path, base = req.path, self.base
        if base and path == base:
            return await self._redirect(writer, req, base + "/")
        if not path.startswith(base + "/"):
            return await self._error(writer, req, 404, "not found")
        rest = path[len(base):]
        if req.method == "POST":
            if rest == "/api/login":
                return await self._login(req, writer)
            if rest == "/api/logout":
                return await self._json(writer, req, 200, {"ok": True}, [("Set-Cookie", self._set_cookie(req, "", 0))])
            return await self._error(writer, req, 405, "method not allowed")
        if req.method not in ("GET", "HEAD"):
            return await self._error(writer, req, 405, "method not allowed")
        if rest == "/":
            return await self._static(req, writer, "index.html")
        if rest == "/sw.js":
            return await self._static(req, writer, "sw.js", [("Service-Worker-Allowed", base + "/")])
        if rest == "/shim.js":
            return await self._static(req, writer, "shim.js")
        if rest.startswith("/static/"):
            return await self._static(req, writer, urllib.parse.unquote(rest[len("/static/"):]))
        if rest.startswith("/d/"):
            return await self._boot(req, writer, rest)
        if rest.startswith("/client/"):
            return await self._client_file(req, writer, rest[len("/client/"):])
        if rest == "/api/health":
            return await self._json(writer, req, 200, {"ok": True})
        if rest == "/api/info":
            return await self._json(writer, req, 200, {
                "name": self.cfg.name, "version": __version__, "base": base,
                "auth": "password" if self.cfg.password else "open", "authorized": self._authorized(req)})
        if rest == "/api/devices":
            denied = self._denied(req)
            if denied:
                return await self._error(writer, req, *denied)
            return await self._json(writer, req, 200, {"devices": self._device_list()})
        if rest.startswith("/api/connect/"):
            return await self._viewer(req, reader, writer, urllib.parse.unquote(rest[len("/api/connect/"):]))
        if rest == "/api/agent":
            return await self._agent(req, reader, writer)
        if rest == "/api/agent/data":
            return await self._data(req, reader, writer)
        return await self._error(writer, req, 404, "not found")

    async def _json(self, writer, req, status, obj, headers=(), close=False):
        body = json.dumps(obj, separators=(",", ":")).encode()
        await respond(writer, status, [("Content-Type", "application/json"), ("Cache-Control", "no-store")]
                      + list(headers), body, head=bool(req and req.method == "HEAD"), close=close)
        return True

    async def _error(self, writer, req, status, message, headers=()):
        return await self._json(writer, req, status, {"error": message}, headers)

    async def _redirect(self, writer, req, location):
        if req.query:
            location += "?" + req.query
        await respond(writer, 302, [("Location", location), ("Cache-Control", "no-cache")], head=req.method == "HEAD")
        return True

    async def _too_many(self, writer, req):
        if req.addr not in self._limited:
            self._limited.add(req.addr)
            log.info("refused %s: too many connections (more than %d in %g s)", req.addr, *self.cfg.rate)
        return await self._error(writer, req, 429, "too many connections from your address - wait a few seconds",
                                 [("Retry-After", "%d" % max(1, round(self.cfg.rate[1])))])

    def _allow(self, addr):
        """Rate limit on new connections (WebSockets, logins) per client address."""
        n, window = self.cfg.rate
        now = time.monotonic()
        q = self.hits.setdefault(addr, deque())
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= n:
            return False
        q.append(now)
        return True

    def _authorized(self, req):
        if not self.cfg.password:
            return True
        auth = req.header("authorization")
        if auth[:7].lower() == "bearer " and hmac.compare_digest(auth[7:].strip().encode(), self.cfg.password.encode()):
            return True
        value = _cookie(req.header("cookie"), COOKIE)
        return bool(value) and hmac.compare_digest(value.encode(), self.cookie.encode())

    def _denied(self, req):
        """Viewer auth -> None, or (status, message). A wrong Bearer password counts against
        the address's rate limit, so passwords cannot be guessed quickly."""
        if self._authorized(req):
            return None
        if req.header("authorization") and not self._allow(req.addr):
            return 429, "too many wrong passwords from your address - wait a few seconds"
        return 401, "log in first"

    def _same_origin(self, req):
        """Browsers send Origin: a page of another web site must not use the viewer's login."""
        origin = req.header("origin")
        if not origin:
            return True
        host = req.header("host")
        if self.cfg.trust_proxy and req.header("x-forwarded-host"):
            host = req.header("x-forwarded-host").split(",")[0].strip()
        return urllib.parse.urlsplit(origin).netloc.lower() == host.lower()

    def _set_cookie(self, req, value, age):
        return "%s=%s; Path=%s/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (
            COOKIE, value, self.base, age, "; Secure" if req.https else "")

    async def _login(self, req, writer):
        if not self._same_origin(req):
            return await self._error(writer, req, 403, "requests from other web sites are not allowed")
        if not self._allow(req.addr):
            return await self._too_many(writer, req)
        if not self.cfg.password:
            return await self._json(writer, req, 200, {"ok": True, "auth": "open"})
        try:
            password = json.loads(req.body or b"{}").get("password")
        except (ValueError, AttributeError):
            password = None
        if not isinstance(password, str) or not hmac.compare_digest(password.encode(), self.cfg.password.encode()):
            log.info("login refused from %s: wrong password", req.addr)
            return await self._error(writer, req, 401, "wrong password")
        log.info("viewer logged in from %s", req.addr)
        return await self._json(writer, req, 200, {"ok": True}, [("Set-Cookie", self._set_cookie(req, self.cookie, COOKIE_AGE))])

    # ---------------------------------------------------------------- files
    def _safe_path(self, root, rel):
        parts = rel.split("/")
        if not rel or "\\" in rel or "\0" in rel or any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            return None
        root = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root, *parts))
        if os.path.commonpath([full, root]) != root or not os.path.isfile(full):
            return None
        return full

    async def _static(self, req, writer, rel, extra=()):
        path = self._safe_path(self.cfg.static, rel)
        if not path:
            return await self._error(writer, req, 404, "not found")
        with open(path, "rb") as f:
            body = f.read()
        ext = os.path.splitext(path)[1].lower()
        headers = [("Content-Type", TYPES.get(ext, "application/octet-stream")), ("Cache-Control", "no-cache")]
        if ext == ".html":      # pages may name the base path as {{BASE}} (the boot page is served at any depth)
            body = body.replace(b"{{BASE}}", html.escape(self.base).encode())
            headers.append(("Content-Security-Policy", CSP))
        return await self._send_body(req, writer, body, headers + list(extra))

    async def _send_body(self, req, writer, body, headers):
        etag = '"%s"' % hashlib.sha256(body).hexdigest()[:24]
        headers.append(("ETag", etag))
        status = 304 if etag in req.header("if-none-match") else 200
        await respond(writer, status, headers, body, head=req.method == "HEAD")
        return True

    async def _boot(self, req, writer, rest):
        """B/d/<id>/<port>/...: the service worker is not in control yet - the boot page installs it."""
        m = BOOT_RE.match(rest)
        if not m or not ID_RE.match(m.group(1)) or not 1 <= int(m.group(2)) <= 65535:
            return await self._error(writer, req, 404, "not found")
        if m.group(3) is None:
            return await self._redirect(writer, req, req.path + "/")
        return await self._static(req, writer, "boot.html")

    async def _client_file(self, req, writer, name):
        """The standalone client for agents (B/client/arstro_relay_client.py, e2e.py, ws.py)."""
        src = CLIENT_FILES.get(name)
        paths = [os.path.join(self.cfg.client_dir, src)] if src else []
        if name == "ws.py":         # repository layout: server/arstro_remote/web/ws.py
            paths.append(os.path.join(self.cfg.client_dir, os.pardir, "web", "ws.py"))
        for path in paths:
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    body = f.read()
                return await self._send_body(req, writer, body, [
                    ("Content-Type", TYPES[".py"]), ("Cache-Control", "no-cache"),
                    ("Content-Disposition", 'attachment; filename="%s"' % name)])
        return await self._error(writer, req, 404, "this device manager has no client file %s" % _text(name))

    # -------------------------------------------------------------- devices
    def _device_list(self):
        out = []
        for dev_id, r in self.devices.items():
            dev = self.online.get(dev_id)
            out.append({"id": dev_id, "name": r.get("name") or dev_id, "hostname": r.get("hostname"),
                        "platform": r.get("platform"), "version": r.get("version"), "slot": r.get("slot"),
                        "web": r.get("web"), "online": dev is not None, "since": int(dev.since) if dev else None,
                        "last_seen": int(dev.heard) if dev else r.get("last_seen"),
                        "viewers": len(self.links.get(dev_id, ()))})
        out.sort(key=lambda d: (not d["online"], str(d["name"]).lower(), d["id"]))
        return out

    def _check_hello(self, hello):
        """-> (error or None, device info)."""
        if not hello or hello.get("t") != "hello":
            return "the first message must be hello", {}
        if hello.get("v") != PROTOCOL:
            return "this device manager speaks relay protocol v%d - update Arstro Remote on the device" % PROTOCOL, {}
        token = hello.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.token.encode()):
            return "wrong join token", {}
        dev_id, name, secret, web = hello.get("id"), hello.get("name"), hello.get("secret"), hello.get("web")
        if not isinstance(dev_id, str) or not ID_RE.match(dev_id):
            return "bad hello: the device id must be 3-40 characters a-z, 0-9 and -", {}
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or not name.isprintable():
            return "bad hello: the name must be 1-64 printable characters", {"id": dev_id}
        if not isinstance(secret, str) or not 32 <= len(secret) <= 512:
            return "bad hello: the secret must be at least 32 characters", {"id": dev_id}
        if not isinstance(web, int) or isinstance(web, bool) or not 1 <= web <= 65535:
            web = None
        return None, {"id": dev_id, "name": name, "hostname": _text(hello.get("hostname")),
                      "platform": _text(hello.get("platform")), "version": _text(hello.get("version")),
                      "slot": _text(hello.get("slot")), "web": web}

    def _register(self, info, secret):
        """Bind the id to sha256(secret) on first sight (TOFU) and store the hello. -> error or None."""
        digest = hashlib.sha256(secret.encode()).hexdigest()
        now = int(time.time())
        error = []

        def change(devices):
            r = devices.get(info["id"])
            if r is None:
                if len(devices) >= MAX_DEVICES:
                    error.append("too many devices at this device manager (%d)" % MAX_DEVICES)
                    return False
                r = devices[info["id"]] = {"secret_sha256": digest, "first_seen": now}
            elif not hmac.compare_digest(str(r.get("secret_sha256", "")), digest):
                error.append("this device id belongs to another device")
                return False
            r.update({k: v for k, v in info.items() if k != "id"}, last_seen=now)
        self._save(change)
        return error[0] if error else None

    async def _ws(self, req, reader, writer):
        try:
            return await accept(req, reader, writer, MAX_MESSAGE)
        except BadRequest as e:
            await self._error(writer, req, e.status, e.message, e.headers)
            return None

    async def _refuse(self, ws, error):
        try:
            await ws.send_json({"t": "error", "error": error})
        except Closed:
            pass
        await ws.close(1008, error[:100])
        return False

    async def _kick(self, dev, reason):
        """Send bye and close a device's control connection."""
        dev.reason = reason
        try:
            await asyncio.wait_for(dev.ws.send_json({"t": "bye", "reason": reason}), 2)
        except (Closed, asyncio.TimeoutError):
            pass
        await dev.ws.close(1000, reason[:100])

    async def _agent(self, req, reader, writer):
        """B/api/agent: a device's control connection (PROTOCOL.md section 2)."""
        if not self._allow(req.addr):
            return await self._too_many(writer, req)
        ws = await self._ws(req, reader, writer)
        if not ws:
            return True
        try:
            hello = await ws.recv_json(FIRST_MESSAGE)
        except (Closed, asyncio.TimeoutError):
            ws.abort()
            return False
        error, info = self._check_hello(hello)
        error = error or self._register(info, hello["secret"])
        if error:
            log.info("device refused from %s: %s%s", req.addr, error, " (id %s)" % info["id"] if info else "")
            return await self._refuse(ws, error)
        dev = Device(info["id"], info["name"], ws, req.addr)
        old = self.online.get(dev.id)
        self.online[dev.id] = dev
        if old:
            self._spawn(self._kick(old, "replaced by a newer connection of the same device"))
        hb = self.cfg.heartbeat
        try:
            await ws.send_json({"t": "welcome", "v": PROTOCOL, "heartbeat": int(hb) if hb == int(hb) else hb,
                                "relay": __version__})
            log.info("device online: %s (%s) from %s, version %s, slot %s", dev.id, dev.name, dev.addr,
                     info["version"], info["slot"])
            while True:
                try:
                    msg = await ws.recv_json(3 * hb)
                except asyncio.TimeoutError:
                    dev.reason = "no heartbeat for %g s" % (3 * hb)
                    ws.abort()
                    break
                dev.heard = time.time()
                t = msg.get("t") if msg else None
                if t == "ping":
                    await ws.send_json({"t": "pong"})
                elif t == "refuse":
                    p = self.pending.get(msg.get("sid")) if isinstance(msg.get("sid"), str) else None
                    if p and p[0] == dev.id and not p[1].done():
                        p[1].set_exception(Refused(_text(msg.get("error"), 200) or "the device refused the connection"))
        except Closed as e:
            dev.reason = dev.reason or ("connection lost" if e.code == 1006 else "closed by the device")
        finally:
            self._offline(dev)
        return False

    def _offline(self, dev):
        if self.online.get(dev.id) is not dev:
            log.info("device connection replaced: %s (%s) from %s", dev.id, dev.name, dev.addr)
            return
        del self.online[dev.id]
        for d, fut in list(self.pending.values()):
            if d == dev.id and not fut.done():
                fut.set_exception(Refused("the device went offline"))
        now = int(time.time())

        def change(devices):
            if dev.id not in devices:
                return False
            devices[dev.id]["last_seen"] = now
        self._save(change)
        log.info("device offline: %s (%s) from %s: %s", dev.id, dev.name, dev.addr, dev.reason)

    async def _data(self, req, reader, writer):
        """B/api/agent/data: a device's data connection for one viewer channel."""
        ws = await self._ws(req, reader, writer)
        if not ws:
            return True
        try:
            msg = await ws.recv_json(FIRST_MESSAGE)
        except (Closed, asyncio.TimeoutError):
            ws.abort()
            return False
        sid, dev_id, p = None, None, None
        if not msg or msg.get("t") != "data":
            error = "the first message must be data"
        elif msg.get("v") != PROTOCOL:
            error = "this device manager speaks relay protocol v%d" % PROTOCOL
        else:
            dev_id, sid = msg.get("id"), msg.get("sid")
            record = self.devices.get(dev_id) if isinstance(dev_id, str) else None
            p = self.pending.get(sid) if isinstance(sid, str) else None
            if record is None or not _secret_ok(record, msg.get("secret")):
                error = "wrong secret for this device"
            elif p is None or p[0] != dev_id or p[1].done():
                error = "unknown or expired channel"
            else:
                error = None
        if error:
            log.info("data connection refused from %s: %s", req.addr,
                     error + (" (device %s)" % dev_id if isinstance(dev_id, str) and ID_RE.match(dev_id) else ""))
            return await self._refuse(ws, error)
        del self.pending[sid]
        done = asyncio.Event()
        p[1].set_result((ws, done))
        await done.wait()           # the viewer's task forwards; this connection lives until it is done
        return False

    # -------------------------------------------------------------- viewers
    async def _viewer(self, req, reader, writer, dev_id):
        """B/api/connect/<id>: join a viewer to a new data connection of the device (section 3)."""
        if not ID_RE.match(dev_id):
            return await self._error(writer, req, 404, "no such device")
        denied = self._denied(req)
        if denied:
            return await self._error(writer, req, *denied)
        if not self._same_origin(req):
            return await self._error(writer, req, 403, "requests from other web sites are not allowed")
        if not self._allow(req.addr):
            return await self._too_many(writer, req)
        ws = await self._ws(req, reader, writer)
        if not ws:
            return True
        dev = self.online.get(dev_id)
        busy = len(self.links.get(dev_id, ())) + sum(1 for d, _ in self.pending.values() if d == dev_id)
        if dev_id not in self.devices:
            return await self._refuse_viewer(ws, req, dev_id, "no device with id %s" % dev_id)
        if dev is None:
            return await self._refuse_viewer(ws, req, dev_id, "the device is offline")
        if busy >= CHANNELS_PER_DEVICE:
            return await self._refuse_viewer(ws, req, dev_id, "the device is busy (%d viewer channels) - try again later"
                                             % CHANNELS_PER_DEVICE)
        sid = secrets.token_urlsafe(16)
        fut = asyncio.get_running_loop().create_future()
        self.pending[sid] = (dev_id, fut)
        try:
            await asyncio.wait_for(dev.ws.send_json({"t": "open", "sid": sid, "addr": req.addr}), 5)
            data, done = await asyncio.wait_for(fut, self.cfg.pending)
        except (asyncio.TimeoutError, Closed):
            return await self._refuse_viewer(ws, req, dev_id, "the device did not answer")
        except Refused as e:
            return await self._refuse_viewer(ws, req, dev_id, str(e))
        finally:
            self.pending.pop(sid, None)
        link = Link(dev_id, ws, data, req.addr)
        self.links.setdefault(dev_id, set()).add(link)
        try:
            await data.send_json({"t": "linked"})
            await ws.send_json({"t": "relay", "ok": True})
            log.info("channel open: device %s, viewer %s", dev_id, req.addr)
            await asyncio.gather(self._pump(ws, data, link, True), self._pump(data, ws, link, False))
        except Closed:
            await asyncio.gather(ws.close(1001), data.close(1001))
        finally:
            done.set()
            links = self.links.get(dev_id, set())
            links.discard(link)
            if not links:
                self.links.pop(dev_id, None)
            log.info("channel closed: device %s, viewer %s, %d bytes to the device, %d bytes to the viewer, %.0f s",
                     dev_id, req.addr, link.up, link.down, time.monotonic() - link.t0)
        return False

    async def _refuse_viewer(self, ws, req, dev_id, error):
        log.info("viewer %s refused for device %s: %s", req.addr, dev_id, error)
        try:
            await ws.send_json({"t": "relay", "ok": False, "error": error})
        except Closed:
            pass
        await ws.close(1000, error[:100])
        return False

    async def _pump(self, src, dst, link, up):
        """Forward src -> dst message by message, unchanged; when src ends, close dst the same way."""
        code, reason = 1000, ""
        try:
            while True:
                op, payload = await src.recv()
                await dst.send(op, payload)
                if up:
                    link.up += len(payload)
                else:
                    link.down += len(payload)
        except Closed as e:
            code, reason = e.code, e.reason
        await dst.close(sendable(code), reason)


def serve(cfg):
    """Run the device manager in the foreground until SIGTERM / SIGINT. -> exit status."""
    try:
        relay = Relay(cfg)
    except OSError as e:
        log.error("cannot use the data directory %s: %s", cfg.data, e)
        return 1
    try:
        asyncio.run(relay.run())
    except OSError as e:
        log.error("cannot listen on %s:%d: %s", cfg.host, cfg.port, e)
        return 1
    return 0
