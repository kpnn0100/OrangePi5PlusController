"""The backup terminal's web server (ADM-09): a login page and shells, nothing else.

    GET  /                      the page (static/index.html)
    GET  /static/<file>         its script, styles and xterm
    GET  /api/ping              {"ok", "server": "arstro-backup", "version", "host"} - no password
    GET  /api/me                200 when logged in, else 401
    POST /api/login             {"password"} -> the login cookie
    POST /api/logout
    GET  /api/terms             {"terms": [{id, shell, created, viewers}]}
    POST /api/terms             {"cols", "rows"} -> {"id"}: a new shell
    POST /api/terms/<id>/close
    GET  /ws/term?id=N[&cols&rows]   the shell's output (binary; first the recent output again),
                                     {"t": "exit", "code"}; in: binary = keys, {"t": "resize", cols, rows}

Everything but /, /static, /api/ping and /api/login needs the password: the HttpOnly cookie
`arstro_backup` (derived from the password, SameSite=Strict) or `Authorization: Bearer <password>`.
"""

import collections
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION
from .vendor import ws

log = logging.getLogger("arstro.backup.web")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
COOKIE = "arstro_backup"
MAX_BODY = 16 * 1024
WRONG_LIMIT = 10                                  # wrong passwords per minute before a pause


def cookie_value(password):
    return hmac.new(password.encode(), b"arstro-backup-cookie-v1", hashlib.sha256).hexdigest()


class Password:
    """The password file, read again when it changes (`arstro-backup password --set`)."""

    def __init__(self, path):
        self.path = path
        self._stamp = None
        self._value = None

    def get(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            try:
                with open(self.path) as f:
                    self._value = f.read().strip() or None
            except OSError:
                self._value = None
            self._stamp = stamp
        return self._value


class BackupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, terms, password, host_name):
        self.terms = terms
        self.password = password
        self.host_name = host_name
        self.wrong = []                            # times of wrong passwords
        self.wrong_lock = threading.Lock()
        super().__init__(addr, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "arstro-backup"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- helpers
    def _send(self, status, body=b"", ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200, extra=None):
        self._send(status, json.dumps(obj).encode(), extra=extra)

    def _error(self, status, message):
        self._json({"ok": False, "error": message}, status)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        if "json" not in (self.headers.get("Content-Type") or ""):
            raise ValueError("send JSON (Content-Type: application/json)")
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("send a JSON object")
        return d

    def _cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _authorized(self):
        pw = self.server.password.get()
        if not pw:
            return False
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), pw.encode()):
            return True
        c = self._cookie()
        return bool(c) and hmac.compare_digest(c.encode(), cookie_value(pw).encode())

    def _static(self, name):
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
            return self._error(404, "not found")
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            data = f.read()
        extra = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                 "Referrer-Policy": "no-referrer"} if ext == ".html" else None
        self._send(200, data, TYPES.get(ext, "application/octet-stream"), extra)

    # ------------------------------------------------------------- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        p = url.path
        if p in ("/", "/index.html"):
            return self._static("index.html")
        if p.startswith("/static/") and re.match(r"^/static/[\w.-]+$", p):
            return self._static(p[len("/static/"):])
        if p == "/api/ping":
            return self._json({"ok": True, "server": "arstro-backup", "version": VERSION, "host": self.server.host_name})
        if not self._authorized():
            return self._error(401, "log in first")
        if p == "/api/me":
            return self._json({"ok": True, "host": self.server.host_name})
        if p == "/api/terms":
            return self._json({"ok": True, "terms": self.server.terms.list()})
        if p == "/ws/term":
            return self._term_socket(urllib.parse.parse_qs(url.query))
        return self._error(404, "not found")

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path).path
        try:
            body = self._body()
        except ValueError as e:
            return self._error(400, str(e))
        if p == "/api/login":
            return self._login(body)
        if p == "/api/logout":
            return self._json({"ok": True}, extra={"Set-Cookie": "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % COOKIE})
        if not self._authorized():
            return self._error(401, "log in first")
        if p == "/api/terms":
            try:
                t = self.server.terms.open(int(body.get("cols") or 80), int(body.get("rows") or 24))
            except (TypeError, ValueError) as e:
                return self._error(400, str(e))
            except OSError as e:
                return self._error(500, "could not start a shell: %s" % e)
            log.info("shell %d opened from %s", t.id, self.address_string())
            return self._json({"ok": True, "id": t.id})
        m = re.match(r"^/api/terms/(\d+)/close$", p)
        if m:
            try:
                self.server.terms.close(int(m.group(1)))
            except KeyError:
                return self._error(404, "no shell %s" % m.group(1))
            return self._json({"ok": True})
        return self._error(404, "not found")

    def _login(self, body):
        srv = self.server
        now = time.monotonic()
        with srv.wrong_lock:
            srv.wrong = [t for t in srv.wrong if now - t < 60]
            if len(srv.wrong) >= WRONG_LIMIT:
                return self._error(429, "too many wrong passwords - wait a minute")
        pw = srv.password.get()
        given = body.get("password")
        if pw and isinstance(given, str) and hmac.compare_digest(given.encode(), pw.encode()):
            log.info("login from %s", self.address_string())
            cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" % (COOKIE, cookie_value(pw))
            return self._json({"ok": True}, extra={"Set-Cookie": cookie})
        with srv.wrong_lock:
            srv.wrong.append(now)
        log.warning("wrong password from %s", self.address_string())
        time.sleep(1)
        return self._error(401, "wrong password")

    # ------------------------------------------------------------- the shell's socket
    def _term_socket(self, q):
        try:
            tid = int((q.get("id") or ["0"])[0])
        except ValueError:
            tid = 0
        t = self.server.terms.get(tid)
        if t is None:
            return self._error(404, "no shell %s - it ended or was closed" % tid)
        key = self.headers.get("Sec-WebSocket-Key")
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
            return self._error(400, "WebSocket upgrade expected")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = ws.WSConnection(self.connection, read=self.rfile.read1)
        try:
            cols, rows = int(q.get("cols", ["0"])[0]), int(q.get("rows", ["0"])[0])
        except ValueError:
            cols = rows = 0
        if cols and rows:
            t.resize(cols, rows)
        v = Viewer(conn)
        v({"t": "hello", "id": t.id, "shell": os.path.basename(t.shell)})
        t.attach(v)
        threading.Thread(target=v.run, name="backup-viewer", daemon=True).start()
        try:
            while True:
                op, data = conn.recv_message()
                if op == ws.OP_BINARY:
                    t.write(data)
                elif op == ws.OP_TEXT:
                    try:
                        m = json.loads(data)
                    except ValueError:
                        continue
                    if m.get("t") == "resize":
                        t.resize(m.get("cols") or 80, m.get("rows") or 24)
                    elif m.get("t") == "in" and isinstance(m.get("d"), str):
                        t.write(m["d"].encode())
        except (OSError, ws.WSClosed, ValueError):
            pass
        finally:
            t.detach(v)
            v.stop()


class Viewer:
    """One browser on a shell: the shell's reader only queues (it must never wait for a slow
    viewer); a thread sends. A viewer that falls LIMIT bytes behind is dropped."""
    LIMIT = 4 << 20

    def __init__(self, conn):
        self.conn = conn
        self.queue = collections.deque()
        self.size = 0
        self.dead = False
        self.cv = threading.Condition()

    def __call__(self, item):
        with self.cv:
            if self.dead:
                return False
            n = len(item) if isinstance(item, (bytes, bytearray)) else 0
            if self.size + n > self.LIMIT:
                self.dead = True
                self.cv.notify()
                return False
            self.queue.append(item)
            self.size += n
            self.cv.notify()
            return True

    def run(self):
        while True:
            with self.cv:
                while not self.queue and not self.dead:
                    self.cv.wait()
                if self.dead:
                    break
                item = self.queue.popleft()
                if not isinstance(item, dict):
                    self.size -= len(item)
            try:
                if isinstance(item, dict):
                    self.conn.send_text(json.dumps(item))
                    if item.get("t") == "exit":
                        break
                else:
                    self.conn.send_binary(item)
            except (OSError, ws.WSClosed):
                break
        self.stop()
        self.conn.close()                      # also ends the handler's receive loop

    def stop(self):
        with self.cv:
            self.dead = True
            self.cv.notify()


def serve(bind, port, terms, password, host_name):
    srv = BackupServer((bind, port), terms, password, host_name)
    log.info("backup terminal on %s:%d", bind or "*", port)
    return srv
