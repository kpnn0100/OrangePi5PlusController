"""HTTP + WebSocket server: the web controller, remote CLI and the app's media link.

  GET  /                      web UI (static files, no token needed to load)
  GET  /api/ping              {"name", "version", "authorized"} - reachability check
  POST /api/login             {"token"} -> HttpOnly cookie for the web UI
  POST /api/logout
  GET  /ws                    WebSocket: the full protocol (same ops/events as Bluetooth)
  GET  /ws/preview            WebSocket: live H.264 preview (see recorder/preview.py)
  GET  /api/media/<file>      a recording (HTTP Range; ?download=1 for "save as")
  GET  /api/thumb/<clip>      JPEG thumbnail of a take
  POST /api/op/<op>           REST shim: run any op with a JSON body -> {"ok", "data"|"error"}

Everything except / and /api/ping needs the token: cookie `arstro_token`,
`Authorization: Bearer <token>` or `?token=<token>` (SEC-03).
"""

import json
import logging
import mimetypes
import os
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

from .. import __version__
from ..session import RestSession
from . import auth, ws

log = logging.getLogger("arstro.web")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "arstro_token"
MIME = {".arh": "application/octet-stream", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
        ".mp4": "video/mp4", ".js": "text/javascript", ".mjs": "text/javascript",
        ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2"}


def local_addresses():
    out = []
    for name, entries in sorted(psutil.net_if_addrs().items()):
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        for a in entries:
            if a.family == socket.AF_INET and not a.address.startswith("169.254."):
                out.append(a.address)
    return out


class WebServer:
    def __init__(self, ctx):
        self.ctx = ctx
        self.host = ctx.config.get("web_host", "0.0.0.0")
        self.port = int(ctx.config.get("web_port", 8080))
        self.token = auth.load()
        self.httpd = None

    # --------------------------------------------------------------- service
    def start(self):
        handler = type("Handler", (Handler,), {"web": self})
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True).start()
        log.info("web server on http://%s:%d/ (%s)", self.host, self.port,
                 ", ".join(local_addresses()) or "no network address")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def info(self, include_token=True):
        d = {"enabled": True, "host": self.host, "port": self.port,
             "urls": ["http://%s:%d/" % (a, self.port) for a in local_addresses()]}
        if include_token:
            d["token"] = self.token
        return d

    def authorized(self, token):
        return auth.check(token, self.token)

    # ------------------------------------------------------------------- ops
    def handle(self, session, op, msg):
        if op == "web.info":
            return self.info(include_token=True)
        if op == "web.rotate_token":
            self.token = auth.rotate()
            log.info("web token rotated by session %d (%s)", session.num, session.controller)
            victims = [s for s in self.ctx.sessions_of(("web", "remote")) if s is not session]

            def kick():
                time.sleep(0.5)
                for s in victims:
                    s.close("token rotated")
            threading.Thread(target=kick, daemon=True).start()
            return self.info(include_token=True)
        raise ValueError("unknown op %s" % op)


class Handler(BaseHTTPRequestHandler):
    web = None                      # WebServer, set by the subclass in WebServer.start
    protocol_version = "HTTP/1.1"
    server_version = "ArstroRemote/" + __version__

    def log_message(self, fmt, *args):   # never log query strings (they may hold the token)
        log.debug("%s %s", self.address_string(), (fmt % args).split("?")[0])

    # ------------------------------------------------------------- helpers
    @property
    def _url(self):
        return urllib.parse.urlsplit(self.path)

    def _token(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[7:].strip()
        q = urllib.parse.parse_qs(self._url.query).get("token")
        if q:
            return q[0]
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return urllib.parse.unquote(v)
        return None

    def _authorized(self):
        return self.web.authorized(self._token())

    def _send_json(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        self._send_json({"ok": False, "error": "token required"}, 401)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode() or "{}")

    # -------------------------------------------------------------- routes
    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        path = self._url.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", head)
            if path.startswith("/assets/"):
                return self._static(path[len("/assets/"):], head)
            if path == "/api/ping":
                return self._send_json({"ok": True, "name": "Arstro Remote", "version": __version__,
                                        "hostname": socket.gethostname(), "authorized": self._authorized()})
            if not self._authorized():
                return self._deny()
            if path == "/ws":
                return self._websocket()
            if path == "/ws/preview":
                return self._preview()
            if path.startswith("/api/media/"):
                return self._media(urllib.parse.unquote(path[len("/api/media/"):]), head)
            if path.startswith("/api/thumb/"):
                return self._thumb(urllib.parse.unquote(path[len("/api/thumb/"):]), head)
            self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = self._url.path
        try:
            if path == "/api/login":
                body = self._body()
                tok = body.get("token", "")
                if not self.web.authorized(tok):
                    time.sleep(0.5)                       # slow down guessing
                    return self._send_json({"ok": False, "error": "wrong token"}, 401)
                cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" % (
                    COOKIE, urllib.parse.quote(tok))
                return self._send_json({"ok": True}, extra={"Set-Cookie": cookie})
            if path == "/api/logout":
                return self._send_json({"ok": True}, extra={
                    "Set-Cookie": "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % COOKIE})
            if not self._authorized():
                return self._deny()
            if path.startswith("/api/op/"):
                return self._rest(path[len("/api/op/"):])
            self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)

    # -------------------------------------------------------------- static
    def _static(self, rel, head):
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self.send_error(HTTPStatus.NOT_FOUND)
        with open(full, "rb") as f:
            data = f.read()
        ext = os.path.splitext(full)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext) or mimetypes.guess_type(full)[0] or
                         "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if rel == "index.html" else "max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    # ----------------------------------------------------------- websocket
    def _upgrade(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if (self.headers.get("Upgrade", "").lower() != "websocket" or not key):
            self.send_error(HTTPStatus.BAD_REQUEST, "WebSocket upgrade expected")
            return None
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return ws.WSConnection(self.connection, read=self.rfile.read1)

    def _websocket(self):
        conn = self._upgrade()
        if conn is None:
            return
        kind = "web" if self.headers.get("Cookie") else "remote"
        session = self.web.ctx.add_session(ws.WSStream(conn), "%s:%d" % self.client_address[:2], kind)
        session._closed.wait()            # keep this handler thread until the session ends

    def _preview(self):
        rec = self.web.ctx.recorder
        if rec is None:
            return self._send_json({"ok": False, "error": "recorder not available"}, 503)
        conn = self._upgrade()
        if conn is None:
            return
        rec.preview.serve(conn, "%s:%d" % self.client_address[:2])

    # --------------------------------------------------------------- media
    def _media(self, name, head):
        rec = self.web.ctx.recorder
        path = rec.media_path(name) if rec else None
        if not path:
            return self.send_error(HTTPStatus.NOT_FOUND)
        download = "download" in urllib.parse.parse_qs(self._url.query)
        self._send_file(path, name, download, head)

    def _thumb(self, clip, head):
        rec = self.web.ctx.recorder
        path = rec.thumbnail(clip) if rec else None
        if not path:
            return self.send_error(HTTPStatus.NOT_FOUND)
        self._send_file(path, os.path.basename(path), False, head, ctype="image/jpeg", cache=True)

    def _send_file(self, path, name, download, head, ctype=None, cache=False):
        try:
            f = open(path, "rb")
        except OSError:
            return self.send_error(HTTPStatus.NOT_FOUND)
        with f:
            st = os.fstat(f.fileno())
            size = st.st_size
            start, end, partial = 0, size - 1, False
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes=") and size:
                try:
                    a, _, b = rng[6:].split(",")[0].partition("-")
                    if a:
                        start, end = int(a), (int(b) if b else size - 1)
                    else:                               # suffix: last N bytes
                        start, end = max(0, size - int(b)), size - 1
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        raise ValueError
                    partial = True
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            length = max(0, end - start + 1)
            self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
            self.send_header("Content-Type", ctype or MIME.get(os.path.splitext(name)[1].lower()) or
                             mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
            self.send_header("Cache-Control", "max-age=300" if cache else "no-cache")
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            disp = "attachment" if download or name.lower().endswith(".arh") else "inline"
            self.send_header("Content-Disposition",
                             "%s; filename*=UTF-8''%s" % (disp, urllib.parse.quote(name)))
            self.end_headers()
            if head:
                return
            self.wfile.flush()
            sock, offset = self.connection.fileno(), start
            try:
                while length > 0:
                    sent = os.sendfile(sock, f.fileno(), offset, min(length, 64 << 20))
                    if sent == 0:
                        break
                    offset += sent
                    length -= sent
            except (BrokenPipeError, ConnectionResetError):
                pass                                    # player seeked / cancelled

    # ---------------------------------------------------------------- REST
    def _rest(self, op):
        msg = self._body()
        msg["op"] = op
        s = RestSession(self.web.ctx, "%s:%d" % self.client_address[:2])
        try:
            data = s.dispatch(msg)
            self._send_json({"ok": True, "data": data})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e).strip("'\"")}, 400)
        finally:
            s.close()
