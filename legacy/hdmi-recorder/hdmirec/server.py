"""Recordings file server: lets other computers browse and download recordings.

    python3 hdmi_recorder.py serve            # http://<board-ip>:8000/

  /                     web page: recordings grouped by take, with RAW / H.265 / FFV1 badges
  /files/<name>         the file (HTTP Range supported: resumable downloads, seeking);
                        add ?download=1 to force "save as"
  /api/recordings       the same list as JSON

Read-only and without login: anyone on the network can download the recordings.
Files are sent with sendfile(), so multi-GB RAW files don't cost CPU.
"""
import html
import json
import mimetypes
import os
import shutil
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import library, settings

MIME = {".arh": "application/octet-stream", ".mkv": "video/x-matroska",
        ".mov": "video/quicktime", ".mp4": "video/mp4"}
BADGE = {"RAW": "#ff9f0a", "H.265": "#0a84ff", "FFV1": "#bf5af2"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HDMI Recorder</title>
<style>
:root {{ --bg:#f5f5f7; --card:#fff; --fg:#1d1d1f; --dim:#6e6e73; --line:#d2d2d7; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#111114; --card:#1c1c1f; --fg:#f2f2f7; --dim:#98989d; --line:#2c2c30; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width:960px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:26px; margin:0 0 4px; }}
.sub {{ color:var(--dim); margin-bottom:20px; }}
.filters a {{ display:inline-block; padding:6px 14px; margin:0 6px 8px 0; border-radius:999px;
  border:1px solid var(--line); color:var(--fg); text-decoration:none; }}
.filters a.on {{ background:#0a84ff; border-color:#0a84ff; color:#fff; }}
.clip {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:14px 16px; margin:12px 0; }}
.clip h2 {{ font-size:17px; margin:0 0 8px; }}
.file {{ display:flex; align-items:center; gap:12px; padding:8px 0; border-top:1px solid var(--line);
  flex-wrap:wrap; }}
.file:first-of-type {{ border-top:0; }}
.badge {{ font-weight:700; font-size:13px; color:#fff; border-radius:7px; padding:3px 9px;
  min-width:52px; text-align:center; }}
.info {{ flex:1; min-width:220px; }}
.name {{ color:var(--dim); font-size:13px; word-break:break-all; }}
.warn {{ color:#ff9f0a; font-size:13px; }}
.btn {{ padding:8px 14px; border-radius:10px; background:#0a84ff; color:#fff;
  text-decoration:none; font-weight:600; white-space:nowrap; }}
.btn.alt {{ background:transparent; color:#0a84ff; border:1px solid #0a84ff; }}
.empty {{ color:var(--dim); padding:40px 0; text-align:center; }}
footer {{ color:var(--dim); font-size:13px; margin-top:28px; }}
</style></head><body><main>
<h1>HDMI Recorder</h1>
<div class="sub">{host} · {count} recordings · {free} free on the board</div>
<div class="filters">{filters}</div>
{body}
<footer>RAW files are .arh (Arstro Raw HDMI): uncompressed frames, read them with
<code>hdmi_recorder.py info / transcode</code>. H.265 (.mp4/.mov) and FFV1 (.mkv) play in
VLC, ffmpeg, DaVinci Resolve. Downloads can be resumed.</footer>
</main></body></html>
"""


class Catalog:
    """Cached recordings list, refreshed at most every few seconds."""

    def __init__(self, folder=None):
        self.fixed = folder             # None: follow the app's storage setting
        self.folder = folder or settings.load()["storage"]
        self.lib = library.Library()
        self.clips = []
        self.stamp = 0.0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if time.monotonic() - self.stamp > 5:
                self.folder = self.fixed or settings.load()["storage"]
                done = threading.Event()
                result = []
                self.lib.scan(self.folder, lambda c: (result.extend(c), done.set()))
                done.wait(60)
                self.clips = result
                self.stamp = time.monotonic()
            return self.clips


def _free(folder):
    try:
        return library._size(shutil.disk_usage(folder).free)
    except OSError:
        return "?"


class Handler(BaseHTTPRequestHandler):
    server_version = "hdmi-recorder"
    catalog = None                  # set by serve()

    def log_message(self, fmt, *args):
        print(f"[http] {self.client_address[0]} {fmt % args}", flush=True)

    # -- routing

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)
        if path in ("/", "/index.html"):
            self._page(query.get("kind", ["all"])[0], head)
        elif path == "/api/recordings":
            self._json(head)
        elif path.startswith("/files/"):
            self._file(path[len("/files/"):], "download" in query, head)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # -- pages

    def _send(self, body, ctype, head):
        data = body.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _page(self, kind, head):
        clips = self.catalog.get()
        filters = "".join(
            f'<a class="{"on" if kind == k else ""}" href="/?kind={urllib.parse.quote(k)}">{label}</a>'
            for k, label in (("all", "All"), ("RAW", "RAW"), ("H.265", "H.265"), ("FFV1", "FFV1")))
        cards = []
        for clip in clips:
            items = [i for i in clip.items if kind == "all" or i.kind == kind]
            if not items:
                continue
            rows = []
            for i in items:
                href = "/files/" + urllib.parse.quote(i.name)
                warn = f'<div class="warn">{html.escape(i.problem)}</div>' if i.problem else ""
                audio = " · with audio" if i.audio else ""
                rows.append(
                    f'<div class="file"><span class="badge" style="background:'
                    f'{BADGE.get(i.kind, "#636366")}">{html.escape(i.kind)}</span>'
                    f'<div class="info">{html.escape(i.summary())}{audio}'
                    f'<div class="name">{html.escape(i.name)}</div>{warn}</div>'
                    f'<a class="btn" href="{href}?download=1">Download</a>'
                    + (f'<a class="btn alt" href="{href}">Open</a>' if i.kind != "RAW" else "")
                    + "</div>")
            cards.append(f'<div class="clip"><h2>{html.escape(clip.title)}</h2>{"".join(rows)}</div>')
        body = "".join(cards) or '<div class="empty">No recordings yet.</div>'
        host = html.escape(f"{socket.gethostname()} · {self.catalog.folder}")
        self._send(PAGE.format(host=host, count=len(clips), free=_free(self.catalog.folder),
                               filters=filters, body=body), "text/html; charset=utf-8", head)

    def _json(self, head):
        out = []
        for clip in self.catalog.get():
            for i in clip.items:
                out.append({"take": clip.title, "name": i.name, "kind": i.kind,
                            "url": "/files/" + urllib.parse.quote(i.name), "size": i.size,
                            "width": i.width, "height": i.height, "fps": round(i.fps, 3),
                            "duration": round(i.duration, 2), "format": i.format,
                            "audio": i.audio, "problem": i.problem})
        self._send(json.dumps(out, indent=1), "application/json", head)

    # -- files

    def _file(self, name, download, head):
        # only plain names of recordings in the folder: no paths, no partial outputs
        if "/" in name or name.startswith(".") or not name.lower().endswith(library.EXTENSIONS):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = os.path.join(self.catalog.folder, name)
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            partial = False
            if rng and rng.startswith("bytes="):
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
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
            ctype = MIME.get(os.path.splitext(name)[1].lower()) or \
                mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(os.fstat(f.fileno()).st_mtime))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            disp = "attachment" if download or name.lower().endswith(".arh") else "inline"
            self.send_header("Content-Disposition",
                             f"{disp}; filename*=UTF-8''{urllib.parse.quote(name)}")
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
                pass                                    # client cancelled / paused


def serve(host="0.0.0.0", port=8000, folder=None):
    Handler.catalog = Catalog(folder)
    os.makedirs(Handler.catalog.folder, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[info] serving {Handler.catalog.folder} on http://{host}:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
