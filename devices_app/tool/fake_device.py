"""A test device for Arstro Devices (MGR-03..05): a small web app on a local port, advertised to
a device manager by the real relay agent (server/arstro_remote/relay/agent.py).

    python3 devices_app/tool/fake_device.py --relay URL --token TOKEN [--id ID] [--name NAME]
                                            [--key-file FILE] [--web-port N]

Prints one JSON line {"id", "key", "port", "closed_port"} once it is online at the device manager
(the key is the access key: tests read it from there - or pass --key-file to keep it out of the
output), then serves until killed. The JVM tests (GatewayTest) and the emulator test use it.

Routes of the web app:
    /               a page (title "Fake device") that sets a cookie and checks a WebSocket echo
    /cookie/set     Set-Cookie: fake=<value of ?v>      /cookie/show   the Cookie header
    /headers        the request headers as JSON          /echo (POST)   the body back
    /big?n=N        N bytes, no Content-Length (the end is the connection's end)
    /download       a text file as an attachment         /ws            a WebSocket echo
"""

import argparse
import http.server
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "server"))

from arstro_remote.relay import agent as ag, e2e  # noqa: E402
from arstro_remote.web import ws as wsmod  # noqa: E402

PAGE = b"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Fake device</title></head><body style="background:#111;color:#eee;font:16px sans-serif">
<h1>Fake device</h1><p id=secure></p><p id=cookie></p><p id=ws>WebSocket: connecting</p>
<p><a id=dl href="/download">Download a file</a> <a id=blob download="blob-export.txt" href="#">Export (blob)</a></p>
<script src="/app.js"></script></body></html>"""

SCRIPT = b"""
document.getElementById('secure').textContent = 'secure context: ' + window.isSecureContext;
fetch('/cookie/set?v=' + Date.now()).then(() => fetch('/cookie/show')).then(r => r.text())
  .then(t => document.getElementById('cookie').textContent = 'cookie: ' + t);
const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
ws.onopen = () => ws.send('ping-' + location.host.split('.')[0].length);
ws.onmessage = (e) => document.getElementById('ws').textContent = 'WebSocket: ' + e.data;
ws.onerror = () => document.getElementById('ws').textContent = 'WebSocket: error';
document.getElementById('blob').addEventListener('click', (e) => {
  e.preventDefault();
  const url = URL.createObjectURL(new Blob(['exported by the fake device\\n'], { type: 'text/plain' }));
  const a = document.createElement('a'); a.href = url; a.download = 'blob-export.txt'; a.click();
  URL.revokeObjectURL(url);
});
"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeDevice/1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/plain", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        args = dict(p.partition("=")[::2] for p in query.split("&") if p)
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/app.js":
            return self.send(200, SCRIPT, "text/javascript")
        if path == "/cookie/set":
            return self.send(200, b"ok", extra=[("Set-Cookie", "fake=%s; Path=/" % args.get("v", "1"))])
        if path == "/cookie/show":
            return self.send(200, (self.headers.get("Cookie") or "").encode())
        if path == "/headers":
            return self.send(200, json.dumps({k.lower(): v for k, v in self.headers.items()}).encode(), "application/json")
        if path == "/big":
            n = int(args.get("n", "1000"))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()                 # HTTP/1.0, no length: the body ends with the connection
            block = bytes(range(256)) * 256
            while n > 0:
                piece = block[:min(n, len(block))]
                self.wfile.write(piece)
                n -= len(piece)
            return
        if path == "/download":
            return self.send(200, b"a file from the fake device\n", extra=[
                ("Content-Disposition", 'attachment; filename="fake-report.txt"')])
        if path == "/ws" and "websocket" in (self.headers.get("Upgrade") or "").lower():
            return self.websocket()
        self.send(404, b"not found")

    def do_POST(self):
        n = self.headers.get("Content-Length")
        if n is None:
            return self.send(411, b"length required")
        body = self.rfile.read(int(n))
        self.send(200, body, "application/octet-stream", extra=[("X-Got", str(len(body)))])

    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsmod.accept_key(key))
        proto = self.headers.get("Sec-WebSocket-Protocol")
        if proto:
            self.send_header("Sec-WebSocket-Protocol", proto.split(",")[0].strip())
        self.end_headers()
        self.wfile.flush()
        conn = wsmod.WSConnection(self.connection, read=self.rfile.read)
        try:
            while True:
                op, data = conn.recv_message()
                if op == wsmod.OP_TEXT and data == b"close-me":
                    conn.close(4001, "asked to close")
                    return
                conn._send(op, data)
        except (wsmod.WSClosed, OSError):
            pass
        self.close_connection = True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--relay", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--id", default="fake-device-%d" % (os.getpid() % 100000))
    p.add_argument("--name", default="Fake device")
    p.add_argument("--key-file", help="write the access key here (0600) instead of printing it")
    p.add_argument("--web-port", type=int, default=0)
    a = p.parse_args()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.web_port), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    key = e2e.new_key()
    agent = ag.Agent()
    agent.configure({"enabled": True, "url": a.relay, "token": a.token, "id": a.id, "name": a.name,
                     "secret": e2e.b64e(os.urandom(32)), "key_raw": e2e.parse_key(key), "hostname": "fake-host",
                     "version": "test", "platform": "Linux test", "slot": "t", "web": port, "ports": None})
    agent.start()
    until = time.time() + 20
    while agent.state != "online":
        if time.time() > until:
            print(json.dumps({"error": "not online: %s" % agent.error}), flush=True)
            return 1
        time.sleep(0.05)
    out = {"id": a.id, "port": port, "closed_port": free_port()}
    if a.key_file:
        fd = os.open(a.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
    else:
        out["key"] = key
    print(json.dumps(out), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        agent.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
