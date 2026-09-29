"""HTTP + WebSocket server: the web controller, remote CLI and the app's media link.

  GET  /                      web UI (static files, no token needed to load)
  GET  /api/ping              {"name", "version", "authorized", "auth", "app"} - reachability check
  GET  /app.apk               the Android app, when setup_pi.sh uploaded one (no password)
  POST /api/login             {"token"} -> HttpOnly cookie for the web UI
  POST /api/logout
  GET  /ws                    WebSocket: the full protocol (same ops/events as Bluetooth)
  GET  /ws/preview            WebSocket: live H.264 preview (see recorder/preview.py)
  GET  /ws/screen             WebSocket: the Pi's desktop, same messages (see screen/service.py)
  GET  /api/media/<file>      a recording (HTTP Range; ?download=1 for "save as")
  GET  /api/thumb/<clip>      JPEG thumbnail of a take
  GET  /api/files/download?path=P    a file of the Files module (FILE-03)
  PUT  /api/files/upload?dir=D&name=N[&overwrite=1]   raw body -> D/N (FILE-02)
  POST /api/op/<op>           REST shim: run any op with a JSON body -> {"ok", "data"|"error"}

Everything except / and /api/ping needs the access password (SEC-03): the login cookie
`arstro_token_<port>` (derived from the password; per port, so A/B slots on one host keep
separate logins), `Authorization: Bearer <password>` or
`?token=<password>`. In open mode (`web.set_auth required=false`) nothing is needed.
Requests from other web sites are refused (Origin must match Host), and in open mode the
Host must be an address or a local name, so a DNS-rebinding page cannot reach the Pi.
"""

import ipaddress
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
from ..paths import app_apk, slot
from ..session import RestSession
from . import auth, ws

log = logging.getLogger("arstro.web")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "arstro_token"          # + "_<port>"
# The UI has no inline scripts; blob: is for the MSE preview player.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")

MIME = {".arh": "application/octet-stream", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
        ".mp4": "video/mp4", ".js": "text/javascript", ".mjs": "text/javascript",
        ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2"}


def _local_host(host):
    """True for Host headers that name this machine on the LAN: an IP address, localhost,
    a single-label or mDNS/home name - not an internet domain (DNS rebinding)."""
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 or host.startswith("[") else host
    name = name.strip("[]")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return bool(name) and ("." not in name or name == socket.gethostname().lower() or
                           name.endswith((".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")))


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
        self.open = auth.is_open()
        self.httpd = None

    # --------------------------------------------------------------- service
    def start(self):
        handler = type("Handler", (Handler,), {"web": self})
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True).start()
        self.ctx.hub.publish("web", self.info(include_token=False))
        log.info("web server on http://%s:%d/ (%s)", self.host, self.port,
                 ", ".join(local_addresses()) or "no network address")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def info(self, include_token=True):
        d = {"enabled": True, "host": self.host, "port": self.port, "auth": "open" if self.open else "password",
             "urls": ["http://%s:%d/" % (a, self.port) for a in local_addresses()]}
        apk, apk_version = app_apk()
        d["app"] = {"path": "/app.apk", "version": apk_version} if apk else None
        if include_token:
            d["token"] = self.token
        return d

    def authorized(self, token):
        return self.open or auth.check(token, self.token)

    def authorized_cookie(self, value):
        return self.open or auth.check_cookie(value, self.token)

    def _kick_others(self, session, reason):
        victims = [s for s in self.ctx.sessions_of(("web", "remote")) if s is not session]

        def kick():
            time.sleep(0.5)
            for s in victims:
                s.close(reason)
        threading.Thread(target=kick, daemon=True).start()

    # ------------------------------------------------------------------- ops
    def handle(self, session, op, msg):
        if op == "web.info":
            return self.info(include_token=True)
        if op == "web.rotate_token":
            self.token = auth.rotate()
            log.info("web password replaced by a random one (session %d, %s)", session.num, session.controller)
            self._kick_others(session, "password changed")
            return self._changed()
        if op == "web.set_password":
            self.token = auth.set_password(msg.get("password"))
            log.info("web password changed by session %d (%s)", session.num, session.controller)
            self._kick_others(session, "password changed")
            return self._changed()
        if op == "web.set_auth":
            required = bool(msg.get("required", True))
            was_open = self.open
            auth.set_open(not required)
            self.open = not required
            log.info("web access %s by session %d (%s)", "needs the password" if required else "is OPEN (no password)",
                     session.num, session.controller)
            if required and was_open:
                self._kick_others(session, "a password is required now")
            return self._changed()
        raise ValueError("unknown op %s" % op)

    def _changed(self):
        self.ctx.hub.publish("web", self.info(include_token=False))
        return self.info(include_token=True)


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
        return None

    @property
    def _cookie_name(self):
        return "%s_%d" % (COOKIE, self.web.port)

    def _cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == self._cookie_name:
                return urllib.parse.unquote(v)
        return None

    def _authorized(self):
        if self.web.open:
            return True
        tok = self._token()
        if tok and self.web.authorized(tok):
            return True
        return self.web.authorized_cookie(self._cookie())

    def _origin_ok(self):
        """Same-origin browsers and non-browser clients only (no cross-site requests,
        no DNS rebinding while the server is open)."""
        host = (self.headers.get("Host") or "").strip().lower()
        origin = self.headers.get("Origin")
        if origin is not None:
            if urllib.parse.urlsplit(origin.strip()).netloc.lower() != host:
                return False
        return not self.web.open or _local_host(host)

    def _forbid(self):
        self._send_json({"ok": False, "error": "cross-site request refused"}, 403)

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
        self._send_json({"ok": False, "error": "password required"}, 401)

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
                apk, apk_version = app_apk()
                return self._send_json({"ok": True, "name": "Arstro Remote", "version": __version__,
                                        "hostname": socket.gethostname(), "authorized": self._authorized(),
                                        "auth": "open" if self.web.open else "password", "slot": slot(),
                                        "app": {"url": "/app.apk", "version": apk_version} if apk else None})
            if path == "/app.apk":                       # public: the app is not a secret (SET-04)
                apk, apk_version = app_apk()
                if not apk:
                    return self.send_error(HTTPStatus.NOT_FOUND, "no app uploaded (run scripts/setup_pi.sh)")
                name = "arstro-remote%s.apk" % ("-v" + apk_version if apk_version else "")
                return self._send_file(apk, name, True, head, ctype="application/vnd.android.package-archive")
            if not self._origin_ok():
                return self._forbid()
            if not self._authorized():
                return self._deny()
            if path == "/ws":
                return self._websocket()
            if path == "/ws/preview":
                return self._preview()
            if path == "/ws/screen":
                return self._screen()
            if path.startswith("/api/media/"):
                return self._media(urllib.parse.unquote(path[len("/api/media/"):]), head)
            if path.startswith("/api/thumb/"):
                return self._thumb(urllib.parse.unquote(path[len("/api/thumb/"):]), head)
            if path == "/api/files/download":
                return self._download(head)
            self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = self._url.path
        try:
            if not self._origin_ok():
                return self._forbid()
            if path == "/api/login":
                body = self._body()
                tok = body.get("password") or body.get("token") or ""
                if not auth.check(tok, self.web.token):
                    time.sleep(0.8)                       # slow down guessing
                    return self._send_json({"ok": False, "error": "wrong password"}, 401)
                cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" % (
                    self._cookie_name, auth.cookie_value(self.web.token))
                return self._send_json({"ok": True}, extra={"Set-Cookie": cookie})
            if path == "/api/logout":
                return self._send_json({"ok": True}, extra={
                    "Set-Cookie": "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % self._cookie_name})
            if not self._authorized():
                return self._deny()
            if path.startswith("/api/op/"):
                return self._rest(path[len("/api/op/"):])
            if path == "/api/files/upload":
                return self._upload()
            self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)

    def do_PUT(self):
        try:
            if not self._origin_ok():
                return self._forbid()
            if not self._authorized():
                return self._deny()
            if self._url.path == "/api/files/upload":
                return self._upload()
            self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --------------------------------------------------------------- files
    def _files(self):
        f = self.web.ctx.files
        if f is None:
            self._send_json({"ok": False, "error": "the Files module is not enabled"}, 404)
        return f

    def _download(self, head):
        files = self._files()
        if files is None:
            return
        from ..session import OpError
        q = urllib.parse.parse_qs(self._url.query)
        try:
            p = files.resolve((q.get("path") or [""])[0])
        except OpError as e:
            return self._send_json({"ok": False, "error": str(e)}, 404)
        if not os.path.isfile(p):
            return self._send_json({"ok": False, "error": "not a file (folders cannot be downloaded)"}, 400)
        log.info("download %s by %s", p, self.address_string())
        self._send_file(p, os.path.basename(p), "inline" not in q, head)

    def _upload(self):
        files = self._files()
        if files is None:
            return
        from ..session import OpError
        q = urllib.parse.parse_qs(self._url.query)
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            return self._send_json({"ok": False, "error": "Content-Length is required"}, 411)
        try:
            target = files.upload_target((q.get("dir") or [""])[0], (q.get("name") or [""])[0],
                                         (q.get("overwrite") or ["0"])[0] in ("1", "true", "yes"))
            entry = files.store(self.rfile, length, target, self.address_string())
        except (OpError, OSError) as e:
            self.close_connection = True              # the body was not (fully) read
            log.warning("upload refused: %s", e)
            return self._send_json({"ok": False, "error": str(e)}, 400)
        self._send_json({"ok": True, "data": entry})

    # -------------------------------------------------------------- static
    def _static(self, rel, head):
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self.send_error(HTTPStatus.NOT_FOUND)
        st = os.stat(full)
        etag = '"%x-%x"' % (int(st.st_mtime_ns // 1000), st.st_size)
        ext = os.path.splitext(full)[1].lower()
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext) or mimetypes.guess_type(full)[0] or
                         "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        # always revalidate (cheap 304s), so an updated server never runs stale UI code
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        if ext == ".html":
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
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
        kind = "web" if self.headers.get("Origin") else "remote"
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

    def _screen(self):
        scr = self.web.ctx.screen
        if scr is None:
            return self._send_json({"ok": False, "error": "remote screen not available"}, 503)
        conn = self._upgrade()
        if conn is None:
            return
        scr.preview.serve(conn, "%s:%d" % self.client_address[:2])

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
