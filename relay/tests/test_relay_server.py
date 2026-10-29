#!/usr/bin/env python3
"""The device manager (relay server): routes, registry, forwarding, limits, admin, pipes (RLY-21..27).

    python3 relay/tests/test_relay_server.py [-k NAME]

Starts the real server (python3 -m arstro_relay) as a subprocess on a free port with a
temporary data directory and plays fake devices and viewers with arstro_remote.web.ws (WebSockets)
and PipeConn below (the HTTP transport). Pages come from a temporary static directory, so the tests
do not depend on the viewer's files. The nginx test (RLY-27) needs Docker; it is skipped without.
"""

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "server", "tests"))
sys.path.insert(0, os.path.join(REPO, "server"))
sys.path.insert(0, os.path.join(REPO, "relay"))

from harness import check, run, test, wait_until  # noqa: E402

from arstro_relay import __version__  # noqa: E402
from arstro_relay import pipe as relay_pipe  # noqa: E402
from arstro_relay import server as relay_server  # noqa: E402
from arstro_remote.relay import e2e  # noqa: E402
from arstro_remote.web import ws as wsmod  # noqa: E402
from nginx_proxy import NginxProxy, NginxUnavailable  # noqa: E402

BASE = "/arstro/internal/devices"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
STATIC_FILES = {
    "index.html": "<!doctype html><title>list</title><meta name=base content='{{BASE}}'><script src='static/app.js'></script>",
    "boot.html": "<!doctype html><title>boot</title><script src='{{BASE}}/static/boot.js'></script>",
    "app.js": "console.log('app')", "boot.js": "console.log('boot')", "style.css": "body{}",
    "sw.js": "self.addEventListener('fetch', () => {})", "shim.js": "window.__shim = 1",
    "sub/nested.js": "1", ".hidden": "hidden",
}


# ------------------------------------------------------------------ helpers
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(url, method="GET", body=None, headers=None, timeout=5):
    """-> (status, headers, body bytes); never follows redirects."""
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if isinstance(body, dict):
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def raw_get(port, target):
    """A request with the path exactly as written (no client-side normalisation) -> status."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % target).encode())
        return int(s.recv(4096).split(b" ")[1])


class Relay:
    """One device manager process."""

    def __init__(self, ctx, **env):
        self.data = env.pop("data", None) or tempfile.mkdtemp(dir=ctx["tmp"])
        self.port = free_port()
        self.base = env.get("RELAY_BASE", BASE)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_")}
        self.env.update(RELAY_HOST="127.0.0.1", RELAY_PORT=str(self.port), RELAY_DATA=self.data,
                        RELAY_STATIC=ctx["static"], RELAY_RATE_LIMIT="100000/10", PYTHONDONTWRITEBYTECODE="1")
        self.env.update(env)
        self.log_path = os.path.join(ctx["tmp"], "relay-%d.log" % self.port)
        self.logf = open(self.log_path, "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "arstro_relay"], cwd=os.path.join(REPO, "relay"),
                                     env=self.env, stdout=self.logf, stderr=self.logf)
        ctx["relays"].append(self)
        ok = wait_until(lambda: self.proc.poll() is not None or self.healthy(), 10)
        check(ok and self.proc.poll() is None, "the relay did not start: %s" % self.log()[-2000:])

    def healthy(self):
        try:
            return http(self.url("/api/health"))[0] == 200
        except OSError:
            return False

    def url(self, path):
        return "http://127.0.0.1:%d%s%s" % (self.port, self.base, path)

    def ws_url(self, path):
        return "ws://127.0.0.1:%d%s%s" % (self.port, self.base, path)

    def token(self):
        if self.env.get("RELAY_JOIN_TOKEN"):
            return self.env["RELAY_JOIN_TOKEN"]
        with open(os.path.join(self.data, "join_token")) as f:
            return f.read().strip()

    def log(self):
        with open(self.log_path) as f:
            return f.read()

    def devices(self, headers=None):
        status, _, body = http(self.url("/api/devices"), headers=headers)
        check(status == 200, "device list: HTTP %d %s" % (status, body[:200]))
        return json.loads(body)["devices"]

    def device(self, dev_id):
        return next((d for d in self.devices() if d["id"] == dev_id), None)

    def admin(self, *args, env=None):
        e = dict(self.env, **(env or {}))
        r = subprocess.run([sys.executable, "-m", "arstro_relay", "admin"] + list(args), cwd=os.path.join(REPO, "relay"),
                           env=e, capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout, r.stderr

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.logf.close()


def wsconnect(url, headers=None):
    c = wsmod.connect(url, headers=headers)
    c.sock.settimeout(5)
    return c


def recv_json(conn):
    op, data = conn.recv_message()
    check(op == wsmod.OP_TEXT, "expected a text message, got opcode %d" % op)
    return json.loads(data)


def expect_closed(conn, what):
    try:
        op, data = conn.recv_message()
    except socket.timeout:
        raise AssertionError("%s: the relay did not close the connection" % what)
    except (wsmod.WSClosed, OSError):
        return
    raise AssertionError("%s: got a message instead of the close: %r" % (what, data[:100]))


def close_code(conn):
    """Read raw frames until the close frame -> its code."""
    while True:
        _, op, data = conn._frame()
        if op == wsmod.OP_CLOSE:
            return struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005


def send_frame(conn, fin, opcode, payload):
    """One masked frame (to build fragmented messages by hand)."""
    n = len(payload)
    head = bytearray([(0x80 if fin else 0) | opcode])
    if n < 126:
        head.append(0x80 | n)
    else:
        head.append(0x80 | 126)
        head += struct.pack(">H", n)
    mask = os.urandom(4)
    conn.sock.sendall(bytes(head) + mask + wsmod._xor(payload, mask))


class FakeDevice:
    """A device's control connection."""

    def __init__(self, relay, dev_id, secret=None, name="Lab Pi", token=None, welcome=True, **extra):
        self.relay, self.id, self.name = relay, dev_id, name
        self.secret = secret or hashlib.sha256(dev_id.encode()).hexdigest()
        self.c = self._connect("/api/agent")
        hello = {"t": "hello", "v": 1, "id": dev_id, "name": name, "secret": self.secret,
                 "token": relay.token() if token is None else token, "version": "2.1.0", "hostname": "pi5",
                 "platform": "linux-aarch64", "slot": "A", "web": 8080}
        hello.update(extra)
        self.c.send_text(json.dumps(hello))
        if welcome:
            self.welcome = self.recv()
            check(self.welcome.get("t") == "welcome", "%s: expected welcome, got %s" % (dev_id, self.welcome))

    def _connect(self, path):
        return wsconnect(self.relay.ws_url(path))

    def recv(self):
        return recv_json(self.c)

    def send(self, obj):
        self.c.send_text(json.dumps(obj))

    def data(self, sid, secret=None, dev_id=None):
        d = self._connect("/api/agent/data")
        d.send_text(json.dumps({"t": "data", "v": 1, "id": dev_id or self.id, "secret": secret or self.secret,
                                "sid": sid}))
        return d

    def close(self):
        self.c.close()


def join(relay, dev, headers=None, viewer=None):
    """Viewer -> relay -> device: a joined channel -> (viewer socket, device data socket).
    viewer: a function that opens the viewer's connection (default: a WebSocket)."""
    v = viewer() if viewer else wsconnect(relay.ws_url("/api/connect/" + dev.id), headers=headers)
    msg = dev.recv()
    check(msg.get("t") == "open" and msg.get("sid"), "expected open, got %s" % msg)
    d = dev.data(msg["sid"])
    check(recv_json(d) == {"t": "linked"}, "device: expected linked")
    check(recv_json(v) == {"t": "relay", "ok": True}, "viewer: expected relay ok")
    return v, d, msg


def refusal(relay, dev_id, headers=None):
    """A viewer that is refused -> the relay's error text."""
    v = wsconnect(relay.ws_url("/api/connect/" + dev_id), headers=headers)
    msg = recv_json(v)
    check(msg.get("t") == "relay" and msg.get("ok") is False, "expected a refusal, got %s" % msg)
    expect_closed(v, "refused viewer")
    return msg["error"]


# ---------------------------------------------------------- pipe helpers
K_TEXT, K_BINARY, K_CLOSE = 1, 2, 8
OP_OF = {K_TEXT: wsmod.OP_TEXT, K_BINARY: wsmod.OP_BINARY}


def pipe_open(base, path, headers=None):
    """POST B/api/pipe/open -> (status, headers, body). base = the relay's URL up to B."""
    return http(base + "/api/pipe/open", "POST", {"path": path}, headers)


def down_records(body):
    """A down answer -> [(kind, seq, payload)]."""
    out, i = [], 0
    while i < len(body):
        kind, seq, n = struct.unpack_from(">BII", body, i)
        out.append((kind, seq, body[i + 9:i + 9 + n]))
        i += 9 + n
    check(i == len(body), "a down answer ends with a whole record")
    return out


def up_body(*records):
    return b"".join(struct.pack(">BI", kind, len(payload)) + payload for kind, payload in records)


class PipeConn:
    """A pipe used like a test WebSocket (send_text, send_binary, recv_message, close). A thread
    long-polls down and acknowledges what it got (poll=False: call down() yourself)."""

    def __init__(self, base, path, headers=None, poll=True):
        status, _, body = pipe_open(base, path, headers)
        if status != 200:
            raise ConnectionError("pipe open: HTTP %d %s" % (status, body[:200]))
        self.info = json.loads(body)
        self.url = base + "/api/pipe/" + self.info["pipe"]
        self.ack = self.seq = 0
        self.q = queue.Queue()
        self.close_code = None
        self.stopped = False
        if poll:
            threading.Thread(target=self._down_loop, daemon=True).start()

    def down(self, ack=None):
        """One down request -> (status, [(kind, seq, payload)] or the error body)."""
        status, h, body = http(self.url + "/down?ack=%d" % (self.ack if ack is None else ack),
                               timeout=self.info["poll"] + 10)
        return status, (down_records(body) if status == 200 else body)

    def _down_loop(self):
        while not self.stopped:
            try:
                status, recs = self.down()
            except OSError as e:
                self.q.put(("error", str(e)))
                return
            if status == 204:
                continue
            if status != 200:
                self.q.put(("gone", status))
                return
            for kind, seq, payload in recs:
                if seq < self.ack:
                    continue                    # sent again (an answer we already had)
                self.ack = seq + 1
                if kind == K_CLOSE:
                    code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                    self.q.put(("close", (code, payload[2:].decode())))
                    try:
                        self.down()             # acknowledge the close: the relay forgets the pipe
                    except OSError:
                        pass
                    return
                self.q.put((OP_OF[kind], payload))

    def recv_message(self, timeout=5):
        try:
            op, data = self.q.get(timeout=timeout)
        except queue.Empty:
            raise socket.timeout("no message on the pipe")
        if op == "close":
            self.close_code = data[0]
            raise wsmod.WSClosed("closed %d %s" % data)
        if op in ("gone", "error"):
            raise wsmod.WSClosed("pipe %s: %s" % (op, data))
        return op, data

    def up(self, *records, seq=None):
        """One up request -> (status, JSON answer)."""
        status, _, body = http(self.url + "/up?seq=%d" % (self.seq if seq is None else seq), "POST", up_body(*records),
                               {"Content-Type": "application/octet-stream"}, timeout=self.info["poll"] + 10)
        return status, json.loads(body) if body else None

    def send(self, *records):
        status, answer = self.up(*records)
        check(status == 200, "pipe up: HTTP %d %s" % (status, answer))
        self.seq = answer["next"]

    def send_text(self, text):
        self.send((K_TEXT, text.encode()))

    def send_binary(self, data):
        self.send((K_BINARY, bytes(data)))

    def close(self, code=1000, reason=""):
        self.send((K_CLOSE, struct.pack(">H", code) + reason.encode()))


def pipe_close_code(conn, timeout=5):
    """Read a PipeConn until its close record -> the code."""
    try:
        while True:
            conn.recv_message(timeout)
    except wsmod.WSClosed:
        return conn.close_code


class PipeDevice(FakeDevice):
    """A device whose control and data connections are pipes (base: the relay's URL up to B)."""

    def __init__(self, relay, dev_id, base=None, **kw):
        self.base = base or relay.url("")
        super().__init__(relay, dev_id, **kw)

    def _connect(self, path):
        return PipeConn(self.base, path)


def pipe_viewer(base, dev_id, headers=None, poll=True):
    return lambda: PipeConn(base, "/api/connect/" + dev_id, headers, poll)


# --------------------------------------------------------------------- tests
@test("RLY-21")
def protocol_constants_match_e2e(ctx):
    check(relay_server.MAX_MESSAGE == e2e.MAX_MESSAGE, "MAX_MESSAGE differs from e2e.py")
    check(set(relay_server.MESSAGES) == set(e2e.RELAY_MESSAGES),
          "relay messages %s != e2e.RELAY_MESSAGES %s" % (sorted(relay_server.MESSAGES), sorted(e2e.RELAY_MESSAGES)))
    check(relay_server.PROTOCOL == e2e.VERSION, "protocol version differs")
    check(relay_server.CHANNELS_PER_DEVICE == 32 and relay_server.MAX_DEVICES == 1000, "limits of PROTOCOL.md section 7")


@test("RLY-21")
def device_list_online_and_offline(ctx):
    r = ctx["main"]
    check(r.devices() == [], "a new device manager lists no devices")
    dev = FakeDevice(r, "lab-pi")
    check(dev.welcome == {"t": "welcome", "v": 1, "heartbeat": 20, "relay": __version__}, "welcome: %s" % dev.welcome)
    d = r.device("lab-pi")
    check(d and d["online"] and d["name"] == "Lab Pi" and d["hostname"] == "pi5" and d["platform"] == "linux-aarch64"
          and d["version"] == "2.1.0" and d["slot"] == "A" and d["web"] == 8080 and d["viewers"] == 0, "listed: %s" % d)
    check(isinstance(d["since"], int) and isinstance(d["last_seen"], int), "since / last_seen: %s" % d)
    status, _, body = http(r.url("/api/devices"))
    check(dev.secret.encode() not in body and b"secret" not in body, "the list must not show secrets")
    with open(os.path.join(r.data, "devices.json")) as f:
        rec = json.load(f)["lab-pi"]
    check(rec["secret_sha256"] == hashlib.sha256(dev.secret.encode()).hexdigest() and dev.secret not in json.dumps(rec),
          "devices.json keeps sha256(secret), never the secret")
    check(all(k in rec for k in ("name", "hostname", "platform", "version", "slot", "web", "first_seen", "last_seen")),
          "devices.json fields: %s" % sorted(rec))
    dev.send({"t": "ping"})
    check(dev.recv() == {"t": "pong"}, "ping -> pong")
    other = FakeDevice(r, "aaa-second", name="Alpha")
    check([x["id"] for x in r.devices()] == ["aaa-second", "lab-pi"], "online devices sorted by name")
    other.close()
    check(wait_until(lambda: not r.device("aaa-second")["online"], 5), "a closed device goes offline")
    check([x["id"] for x in r.devices()] == ["lab-pi", "aaa-second"], "online first, then offline")
    dev.close()
    check(wait_until(lambda: not r.device("lab-pi")["online"], 5), "lab-pi goes offline")
    d = r.device("lab-pi")
    check(d["since"] is None and isinstance(d["last_seen"], int), "offline: since null, last_seen kept: %s" % d)


@test("RLY-21")
def join_token_and_bad_hello_refused(ctx):
    r = ctx["main"]
    cases = [({"token": "not-the-token"}, "wrong join token"),
             ({"v": 2}, "protocol v1"),
             ({"id": "Bad_ID"}, "bad hello"),
             ({"name": ""}, "bad hello"),
             ({"secret": "short"}, "bad hello")]
    for extra, want in cases:
        dev = FakeDevice(r, "tok-pi", welcome=False, **extra)
        msg = dev.recv()
        check(msg.get("t") == "error" and want in msg.get("error", ""), "%s: expected '%s', got %s" % (extra, want, msg))
        expect_closed(dev.c, "refused device")
    c = wsconnect(r.ws_url("/api/agent"))
    c.send_binary(b"\x00\x01")
    check(recv_json(c).get("error") == "the first message must be hello", "a binary first message is refused")
    check(r.device("tok-pi") is None, "refused devices are not registered")
    check("not-the-token" not in r.log(), "a wrong token is not logged")


@test("RLY-21")
def device_id_bound_to_its_secret(ctx):
    r = ctx["main"]
    first = FakeDevice(r, "bound-pi", secret="a" * 40)
    thief = FakeDevice(r, "bound-pi", secret="b" * 40, welcome=False)
    msg = thief.recv()
    check(msg == {"t": "error", "error": "this device id belongs to another device"}, "other secret: %s" % msg)
    d = first.data("no-such-sid", secret="b" * 40)
    check(recv_json(d) == {"t": "error", "error": "wrong secret for this device"}, "data socket with the wrong secret")
    again = FakeDevice(r, "bound-pi", secret="a" * 40)
    bye = first.recv()
    check(bye.get("t") == "bye" and "replaced" in bye.get("reason", ""), "the old connection gets bye: %s" % bye)
    expect_closed(first.c, "replaced connection")
    time.sleep(0.3)
    check(r.device("bound-pi")["online"], "the newer connection stays online")
    again.close()


@test("RLY-21")
def viewer_channel_forwards_unchanged(ctx):
    r = ctx["main"]
    dev = FakeDevice(r, "fwd-pi")
    v, d, opened = join(r, dev)
    check(opened["addr"] == "127.0.0.1", "open carries the viewer address: %s" % opened)
    check(r.device("fwd-pi")["viewers"] == 1, "the list counts the viewer")
    up = bytes(range(256)) * 300 + b"\x00\xff"
    v.send_binary(up)
    check(d.recv_message() == (wsmod.OP_BINARY, up), "viewer -> device binary unchanged")
    down = os.urandom(70000)
    d.send_binary(down)
    check(v.recv_message() == (wsmod.OP_BINARY, down), "device -> viewer binary unchanged")
    text = '{"t":"hello","v":1,"cn":"héllo-marker-7f3a"}'
    v.send_text(text)
    check(d.recv_message() == (wsmod.OP_TEXT, text.encode()), "viewer -> device text unchanged")
    d.send_text(text)
    check(v.recv_message() == (wsmod.OP_TEXT, text.encode()), "device -> viewer text unchanged")
    big = os.urandom(e2e.MAX_MESSAGE)
    v.send_binary(big)
    check(d.recv_message() == (wsmod.OP_BINARY, big), "a message of exactly MAX_MESSAGE passes")
    send_frame(v, False, wsmod.OP_TEXT, b"frag-1 ")
    send_frame(v, False, wsmod.OP_CONT, b"frag-2 ")
    send_frame(v, True, wsmod.OP_PING, b"p")
    send_frame(v, True, wsmod.OP_CONT, b"frag-3")
    check(d.recv_message() == (wsmod.OP_TEXT, b"frag-1 frag-2 frag-3"), "fragments arrive as one message")
    check(v._frame() == (True, wsmod.OP_PONG, b"p"), "a ping is answered with pong")
    v.close(4001)
    check(close_code(d) == 4001, "the viewer's close reaches the device with its code")
    check(wait_until(lambda: r.device("fwd-pi")["viewers"] == 0, 5), "the channel is gone from the list")
    n_up = len(up) + len(text.encode()) + len(big) + len(b"frag-1 frag-2 frag-3")
    check(wait_until(lambda: "channel closed: device fwd-pi, viewer 127.0.0.1, %d bytes to the device" % n_up in r.log(), 5),
          "the log counts the bytes")
    check("marker-7f3a" not in r.log(), "forwarded content is never logged")
    v, d, _ = join(r, dev)
    d.close(1000)
    expect_closed(v, "the device side closed -> the viewer is closed")
    dev.close()


@test("RLY-21")
def viewer_refused_when_device_cannot_take_it(ctx):
    r = ctx["main"]
    check("no device with id nobody-here" in refusal(r, "nobody-here"), "unknown device")
    try:
        wsconnect(r.ws_url("/api/connect/Bad_Id"))
        raise AssertionError("an invalid id must be refused")
    except ConnectionError as e:
        check("404" in str(e), "invalid id -> 404: %s" % e)
    gone = FakeDevice(r, "gone-pi")
    gone.close()
    check(wait_until(lambda: not r.device("gone-pi")["online"], 5), "gone-pi offline")
    check(refusal(r, "gone-pi") == "the device is offline", "offline device")
    dev = FakeDevice(r, "refuse-pi")
    v = wsconnect(r.ws_url("/api/connect/refuse-pi"))
    msg = dev.recv()
    dev.send({"t": "refuse", "sid": msg["sid"], "error": "too many viewers - wait a minute"})
    check(recv_json(v) == {"t": "relay", "ok": False, "error": "too many viewers - wait a minute"},
          "the device's refuse text reaches the viewer")
    expect_closed(v, "refused viewer")
    d = dev.data(msg["sid"])
    check(recv_json(d) == {"t": "error", "error": "unknown or expired channel"}, "a refused sid cannot be used")
    v, d, opened = join(r, dev)
    d2 = dev.data(opened["sid"])
    check(recv_json(d2) == {"t": "error", "error": "unknown or expired channel"}, "a sid is used only once")
    v.close()
    d.close()
    check(wait_until(lambda: r.device("refuse-pi")["viewers"] == 0, 5), "the channel ended")
    viewers = [wsconnect(r.ws_url("/api/connect/refuse-pi")) for _ in range(32)]
    for _ in viewers:
        check(dev.recv().get("t") == "open", "open for each waiting viewer")
    check("busy" in refusal(r, "refuse-pi"), "the 33rd channel is refused")
    for x in viewers:
        x.close()
    dev.close()


@test("RLY-21")
def oversized_message_closes_with_1009(ctx):
    r = ctx["main"]
    dev = FakeDevice(r, "big-pi")
    v, d, _ = join(r, dev)
    try:
        v.send_binary(b"x" * (e2e.MAX_MESSAGE + 1))
    except OSError:
        pass
    check(close_code(v) == 1009, "the sender is closed with 1009")
    check(close_code(d) == 1009, "the other side is closed too")
    dev.close()


@test("RLY-21")
def websocket_protocol_errors_close_the_channel(ctx):
    r = ctx["main"]
    dev = FakeDevice(r, "proto-pi")
    for frame, code in ((b"\x82\x03abc", 1002),                       # unmasked frame from a client
                        (b"\x83\x80\x00\x00\x00\x00", 1002),           # unknown opcode
                        (b"\x81\x82\x00\x00\x00\x00\xc3\x28", 1007)):  # text that is not UTF-8
        v, d, _ = join(r, dev)
        v.sock.sendall(frame)
        check(close_code(v) == code, "%r -> close %d" % (frame, code))
        check(close_code(d) == code, "the device side is closed too")
    dev.close()


@test("RLY-21")
def heartbeat_and_pending_timeouts(ctx):
    r = Relay(ctx, RELAY_HEARTBEAT="1", RELAY_PENDING="1")
    quiet = FakeDevice(r, "quiet-pi")
    check(quiet.welcome["heartbeat"] == 1, "welcome carries the heartbeat: %s" % quiet.welcome)
    alive = FakeDevice(r, "alive-pi")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 4.5:
        alive.send({"t": "ping"})
        check(alive.recv() == {"t": "pong"}, "pong")
        time.sleep(0.5)
    check(not r.device("quiet-pi")["online"], "3 silent heartbeats: the device is dropped")
    check(r.device("alive-pi")["online"], "a device that pings stays online")
    expect_closed(quiet.c, "silent device")
    check("no heartbeat" in r.log(), "the drop is logged")
    v = wsconnect(r.ws_url("/api/connect/alive-pi"))
    check(alive.recv().get("t") == "open", "open sent")
    t0 = time.monotonic()
    check(recv_json(v) == {"t": "relay", "ok": False, "error": "the device did not answer"}, "no data connection")
    check(time.monotonic() - t0 < 3, "refused after the pending time")
    alive.close()
    r.stop()


@test("RLY-23")
def viewer_password(ctx):
    pw = "pw-123 secret!"
    r = Relay(ctx, RELAY_PASSWORD=pw, RELAY_TRUST_PROXY="1", RELAY_NAME="Lab devices")
    info = json.loads(http(r.url("/api/info"))[2])
    check(info == {"name": "Lab devices", "version": __version__, "base": BASE, "auth": "password",
                   "authorized": False}, "info: %s" % info)
    check(http(r.url("/api/devices"))[0] == 401, "the list needs the password")
    try:
        wsconnect(r.ws_url("/api/connect/pw-pi"))
        raise AssertionError("a viewer socket needs the password")
    except PermissionError:
        pass
    check(http(r.url("/api/login"), "POST", {"password": "wrong"})[0] == 401, "wrong password -> 401")
    status, h, _ = http(r.url("/api/login"), "POST", {"password": pw})
    cookie = h.get("Set-Cookie", "")
    check(status == 200 and cookie.startswith("arstro_devices="), "login sets the cookie: %s" % cookie)
    for attr in ("Path=%s/" % BASE, "HttpOnly", "SameSite=Strict"):
        check(attr in cookie, "cookie has %s: %s" % (attr, cookie))
    check("Secure" not in cookie, "no Secure flag over plain http")
    _, h, _ = http(r.url("/api/login"), "POST", {"password": pw}, {"X-Forwarded-Proto": "https"})
    check("; Secure" in h.get("Set-Cookie", ""), "Secure behind an https proxy")
    jar = {"Cookie": cookie.split(";")[0]}
    check(http(r.url("/api/devices"), headers=jar)[0] == 200, "the cookie opens the list")
    check(json.loads(http(r.url("/api/info"), headers=jar)[2])["authorized"], "info: authorized with the cookie")
    bearer = {"Authorization": "Bearer " + pw}
    check(http(r.url("/api/devices"), headers=bearer)[0] == 200, "Bearer works")
    check(http(r.url("/api/devices"), headers={"Authorization": "Bearer nope"})[0] == 401, "wrong Bearer -> 401")
    check(http(r.url("/api/devices"), headers={"Cookie": "arstro_devices=forged"})[0] == 401, "forged cookie -> 401")
    dev = FakeDevice(r, "pw-pi")
    v, d, opened = join(r, dev, headers=dict(jar, **{"X-Forwarded-For": "198.51.100.66, 203.0.113.7"}))
    check(opened["addr"] == "203.0.113.7",
          "RELAY_TRUST_PROXY: the last X-Forwarded-For entry (the proxy's), not a faked first one: %s" % opened)
    v.close()
    try:
        wsconnect(r.ws_url("/api/connect/pw-pi"), headers=dict(bearer, Origin="https://evil.example"))
        raise AssertionError("a viewer socket from another web site must be refused")
    except ConnectionError as e:
        check("403" in str(e), "other origin -> 403: %s" % e)
    check(http(r.url("/api/login"), "POST", {"password": pw}, {"Origin": "https://evil.example"})[0] == 403,
          "login from another web site -> 403")
    _, h, _ = http(r.url("/api/logout"), "POST", b"")
    check("Max-Age=0" in h.get("Set-Cookie", ""), "logout clears the cookie")
    dev.close()
    check(pw not in r.log() and dev.secret not in r.log(), "passwords and secrets are never logged")
    r.stop()
    open_info = json.loads(http(ctx["main"].url("/api/info"))[2])
    check(open_info["auth"] == "open" and open_info["authorized"], "without RELAY_PASSWORD the list is open")


@test("RLY-24")
def rate_limit_on_new_connections(ctx):
    r = Relay(ctx, RELAY_TRUST_PROXY="1", RELAY_RATE_LIMIT="", RELAY_PASSWORD="pw-rate")
    me = {"X-Forwarded-For": "198.51.100.1", "Authorization": "Bearer pw-rate"}
    for i in range(30):
        c = wsconnect(r.ws_url("/api/connect/nobody-here"), headers=me)
        c.close()
    try:
        wsconnect(r.ws_url("/api/connect/nobody-here"), headers=me)
        raise AssertionError("the 31st connection in 10 s must be refused")
    except ConnectionError as e:
        check("429" in str(e), "-> 429: %s" % e)
    check(http(r.url("/api/login"), "POST", {"password": "x"}, me)[0] == 429, "logins count too")
    other = wsconnect(r.ws_url("/api/connect/nobody-here"),
                      headers={"X-Forwarded-For": "198.51.100.2", "Authorization": "Bearer pw-rate"})
    check(recv_json(other)["ok"] is False, "another address is not limited")
    check(http(r.url("/api/health"), headers=me)[0] == 200, "plain pages are not limited")
    guess = {"X-Forwarded-For": "198.51.100.3", "Authorization": "Bearer guess"}
    codes = [http(r.url("/api/devices"), headers=guess)[0] for _ in range(31)]
    check(codes == [401] * 30 + [429], "wrong Bearer passwords are rate limited: %s" % codes)
    check("too many connections" in r.log(), "the refusal is logged")
    r.stop()


@test("RLY-24")
def security_headers(ctx):
    r = ctx["main"]
    for path in ("/", "/d/lab-pi/8080/x"):
        status, h, _ = http(r.url(path))
        check(status == 200 and h.get("Content-Security-Policy") == CSP, "%s: CSP %r" % (path, h.get("Content-Security-Policy")))
        check(h.get("Content-Type") == "text/html; charset=utf-8", "%s: html" % path)
    for path in ("/", "/api/info", "/static/app.js", "/sw.js", "/no/such/thing", "/client/e2e.py"):
        status, h, _ = http(r.url(path))
        check(h.get("X-Content-Type-Options") == "nosniff" and h.get("Referrer-Policy") == "no-referrer",
              "%s: nosniff + no-referrer" % path)


@test("RLY-24")
def base_path_and_traversal(ctx):
    r = ctx["main"]
    for target in ("/static/../server.py", "/static/..%2f..%2fserver.py", "/static/%2e%2e/websocket.py",
                   "/static/.hidden", "/static/", "/static/sub", "/static/..%5cx", "/static/a%00.js",
                   "/static/../../arstro_relay/server.py"):
        check(raw_get(r.port, BASE + target) == 404, "%s must be 404" % target)
    check(http(r.url("/static/sub/nested.js"))[0] == 200, "nested static files are served")
    check(http("http://127.0.0.1:%d/api/health" % r.port)[0] == 404, "nothing outside the base path")
    other = os.path.join(ctx["tmp"], "pkgclient")
    os.makedirs(other, exist_ok=True)
    for name in ("client.py", "e2e.py", "ws.py", "pipe.py"):
        with open(os.path.join(other, name), "w") as f:
            f.write("# packaged %s\n" % name)
    b = Relay(ctx, RELAY_BASE="/x/devs", RELAY_CLIENT_DIR=other)
    check(http("http://127.0.0.1:%d/x/devs/api/health" % b.port)[0] == 200, "RELAY_BASE is honoured")
    check(http("http://127.0.0.1:%d%s/api/health" % (b.port, BASE))[0] == 404, "the default base is gone")
    status, h, _ = http("http://127.0.0.1:%d/x/devs?a=1" % b.port)
    check(status == 302 and h.get("Location") == "/x/devs/?a=1", "B -> B/: %s %s" % (status, h.get("Location")))
    check(json.loads(http(b.url("/api/info"))[2])["base"] == "/x/devs", "info names the base")
    check(http(b.url("/sw.js"))[1].get("Service-Worker-Allowed") == "/x/devs/", "sw.js scope follows the base")
    check(b"'/x/devs/static/boot.js'" in http(b.url("/d/lab-pi/80/"))[2], "{{BASE}} in pages becomes the base")
    for name in ("arstro_relay_client.py", "e2e.py", "ws.py", "pipe.py"):
        status, _, body = http(b.url("/client/" + name))
        want = {"arstro_relay_client.py": "client.py"}.get(name, name)
        check(status == 200 and body == ("# packaged %s\n" % want).encode(), "packaged client file %s" % name)
    b.stop()
    for bad in ("/a/", "relay", "/a/../b", "/a b"):
        p = subprocess.run([sys.executable, "-m", "arstro_relay"], cwd=os.path.join(REPO, "relay"),
                           env=dict(r.env, RELAY_BASE=bad, RELAY_PORT=str(free_port())),
                           capture_output=True, text=True, timeout=10)
        check(p.returncode == 2 and "RELAY_BASE must start with /" in p.stderr, "RELAY_BASE=%r refused: %s" % (bad, p.stderr))


@test("RLY-22")
def static_routes(ctx):
    r = ctx["main"]
    status, h, body = http(r.url("/"))
    check(status == 200 and b"<title>list</title>" in body and ("content='%s'" % BASE).encode() in body,
          "B/ is index.html with the base filled in")
    status, h, body = http(r.url("/static/app.js"))
    check(status == 200 and body == b"console.log('app')" and h.get("Content-Type").startswith("text/javascript"),
          "app.js as text/javascript")
    check(http(r.url("/static/style.css"))[1].get("Content-Type").startswith("text/css"), "css")
    status, h, body = http(r.url("/sw.js"))
    check(status == 200 and h.get("Service-Worker-Allowed") == BASE + "/" and h.get("Cache-Control") == "no-cache"
          and h.get("Content-Type").startswith("text/javascript"), "sw.js headers: %s" % dict(h))
    check(http(r.url("/shim.js"))[2] == b"window.__shim = 1", "shim.js")
    for path in ("/d/lab-pi/8080/", "/d/lab-pi/8080/some/page.html?x=1", "/d/abc/1/a/b/c/"):
        status, h, body = http(r.url(path))
        check(status == 200 and b"<title>boot</title>" in body and ("'%s/static/boot.js'" % BASE).encode() in body,
              "%s is the boot page" % path)
    status, h, _ = http(r.url("/d/lab-pi/8080"))
    check(status == 302 and h.get("Location") == BASE + "/d/lab-pi/8080/", "B/d/<id>/<port> -> .../")
    for path in ("/d/Bad_Id/8080/", "/d/lab-pi/70000/", "/d/lab-pi/0/", "/d/lab-pi/x/", "/d/lab-pi/"):
        check(http(r.url(path))[0] == 404, "%s -> 404" % path)
    check(http(r.url("/d/lab-pi/8080/form"), "POST", b"a=1")[0] == 405, "POST to a device page -> 405")
    status, h, _ = http(r.url("/static/app.js"))
    check(http(r.url("/static/app.js"), headers={"If-None-Match": h["ETag"]})[0] == 304, "ETag -> 304")
    status, h, body = http(r.url("/static/app.js"), "HEAD")
    check(status == 200 and body == b"" and h.get("Content-Length") == str(len("console.log('app')")), "HEAD")


@test("RLY-26")
def client_download(ctx):
    r = ctx["main"]
    src = os.path.join(REPO, "server", "arstro_remote")
    check(os.path.exists(os.path.join(src, "relay", "pipe.py")), "the client's HTTP transport relay/pipe.py exists")
    for name, path in (("e2e.py", "relay/e2e.py"), ("ws.py", "web/ws.py"), ("arstro_relay_client.py", "relay/client.py"),
                       ("pipe.py", "relay/pipe.py")):
        status, h, body = http(r.url("/client/" + name))
        full = os.path.join(src, path)
        if not os.path.exists(full):
            check(status == 404, "%s: 404 while %s does not exist" % (name, path))
            continue
        with open(full, "rb") as f:
            check(status == 200 and body == f.read(), "%s is %s" % (name, path))
        check(h.get("Content-Type").startswith("text/x-python") and name in h.get("Content-Disposition", ""),
              "%s: python file download" % name)
    for name in ("client.py", "other.py", "..%2fe2e.py"):
        check(http(r.url("/client/" + name))[0] == 404, "%s -> 404" % name)


@test("RLY-21")
def max_devices(ctx):
    data = tempfile.mkdtemp(dir=ctx["tmp"])
    seeded = {"dev-%04d" % i: {"name": "d%d" % i, "secret_sha256": hashlib.sha256(b"x" * 40).hexdigest()}
              for i in range(1000)}
    with open(os.path.join(data, "devices.json"), "w") as f:
        json.dump(seeded, f)
    r = Relay(ctx, data=data)
    dev = FakeDevice(r, "one-too-many", welcome=False)
    check(dev.recv() == {"t": "error", "error": "too many devices at this device manager (1000)"}, "1001st device")
    known = FakeDevice(r, "dev-0007", secret="x" * 40)
    check(r.device("dev-0007")["online"], "a known device still joins")
    known.close()
    r.stop()


@test("RLY-25")
def admin_forget_and_token_rotate(ctx):
    r = ctx["main"]
    dev = FakeDevice(r, "admin-pi", name="Admin Pi")
    code, out, _ = r.admin("list")
    check(code == 0 and "admin-pi" in out and "Admin Pi" in out, "admin list: %s" % out)
    code, out, _ = r.admin("list", "--json")
    check(code == 0 and "admin-pi" in out and "secret" not in out, "admin list --json without secrets")
    code, out, err = r.admin("forget", "nobody-here")
    check(code == 1 and "no device" in err, "forget an unknown id")
    code, out, _ = r.admin("forget", "admin-pi")
    check(code == 0 and "forgot admin-pi" in out, "forget: %s" % out)
    t0 = time.monotonic()
    bye = dev.recv()
    check(bye.get("t") == "bye" and "removed" in bye.get("reason", "") and time.monotonic() - t0 < 5,
          "the forgotten device gets bye within 5 s: %s" % bye)
    expect_closed(dev.c, "forgotten device")
    check(r.device("admin-pi") is None, "gone from the list")
    again = FakeDevice(r, "admin-pi", secret="c" * 40)
    check(r.device("admin-pi")["online"], "the id is free again (another secret may take it)")
    again.close()
    old = r.token()
    code, out, _ = r.admin("token")
    check(code == 0 and out.strip() == old, "admin token prints the token")
    code, out, _ = r.admin("token", "--rotate")
    new = out.strip()
    check(code == 0 and new and new != old and r.token() == new, "rotate makes a new token")

    def old_refused():
        d = FakeDevice(r, "rot-pi", token=old, welcome=False)
        msg = d.recv()
        d.close()
        return msg.get("error") == "wrong join token"
    check(wait_until(old_refused, 5, 0.5), "the running server refuses the old token within 5 s")
    d = FakeDevice(r, "rot-pi", token=new)
    d.close()
    code, _, err = r.admin("token", "--rotate", env={"RELAY_JOIN_TOKEN": "from-env"})
    check(code == 1 and "RELAY_JOIN_TOKEN" in err, "no rotation when the token comes from the environment")


@test("RLY-21")
def shutdown_says_bye(ctx):
    r = Relay(ctx, RELAY_JOIN_TOKEN="env-token-123")
    check("devices join with the token from RELAY_JOIN_TOKEN" in r.log() and "env-token-123" not in r.log(),
          "a token from the environment is not logged")
    dev = FakeDevice(r, "bye-pi")
    r.proc.send_signal(signal.SIGTERM)
    bye = dev.recv()
    check(bye == {"t": "bye", "reason": "the device manager is shutting down"}, "bye on shutdown: %s" % bye)
    check(r.proc.wait(5) == 0, "exits cleanly")
    r.stop()


@test("RLY-21")
def log_lines(ctx):
    r = ctx["main"]
    dev = FakeDevice(r, "log-pi", name="Log Pi")
    v, d, _ = join(r, dev)
    v.send_text("secret-content-55aa")
    d.recv_message()
    v.close()
    FakeDevice(r, "log-pi", token="wrong-token-for-log", welcome=False).recv()
    dev.close()
    check(wait_until(lambda: not r.device("log-pi")["online"], 5), "log-pi offline")
    check(refusal(r, "log-pi") == "the device is offline", "refused viewer")
    log = r.log()
    first = next(line for line in log.splitlines() if " relay: " in line)
    check(re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO relay: device manager %s " % re.escape(__version__), first),
          "log format 'time level component: message': %s" % first)
    for what in ("device online: log-pi (Log Pi) from 127.0.0.1", "device offline: log-pi (Log Pi) from 127.0.0.1",
                 "channel open: device log-pi, viewer 127.0.0.1", "channel closed: device log-pi, viewer 127.0.0.1",
                 "viewer 127.0.0.1 refused for device log-pi: the device is offline",
                 "device refused from 127.0.0.1: wrong join token"):
        check(wait_until(lambda: what in r.log(), 5), "logged: %s" % what)
    log = r.log()
    for secret in (dev.secret, "wrong-token-for-log", "secret-content-55aa"):
        check(secret not in log, "never logged: %s" % secret)
    check(log.count(r.token()) <= 1, "the join token appears at most in the start-up line")


# ------------------------------------------------------- HTTP transport (pipes)
@test("RLY-27")
def pipe_constants_match_e2e(ctx):
    for name in ("PIPE_MAX_UP", "PIPE_POLL", "PIPE_IDLE", "PIPE_BUFFER"):
        check(getattr(relay_pipe, name) == getattr(e2e, name), "%s differs from e2e.py" % name)
    check(relay_pipe.DOWN_MAX == e2e.MAX_MESSAGE, "a down answer holds up to MAX_MESSAGE")


@test("RLY-27")
def pipe_device_hello_and_welcome(ctx):
    r = ctx["main"]
    c = PipeConn(r.url(""), "/api/agent", poll=False)
    check(c.info == {"pipe": c.info["pipe"], "max_up": e2e.PIPE_MAX_UP, "poll": e2e.PIPE_POLL}, "open: %s" % c.info)
    check(re.match(r"^[A-Za-z0-9_-]{22}$", c.info["pipe"]), "the id is 128 random bits, base64url: %s" % c.info["pipe"])
    hello = {"t": "hello", "v": 1, "id": "pipe-pi", "name": "Pipe Pi", "secret": "p" * 40, "token": r.token(),
             "version": "2.1.0", "hostname": "pi5", "platform": "linux-aarch64", "slot": "B", "web": 8080}
    c.send_text(json.dumps(hello))
    status, h, body = http(c.url + "/down?ack=0", timeout=10)
    check(status == 200 and h.get("Content-Type") == "application/octet-stream" and h.get("Cache-Control") == "no-store"
          and h.get("X-Accel-Buffering") == "no" and h.get("Content-Length") == str(len(body)), "down headers: %s" % dict(h))
    recs = down_records(body)
    check(len(recs) == 1 and recs[0][:2] == (K_TEXT, 0), "one text record, seq 0: %s" % recs)
    check(json.loads(recs[0][2]) == {"t": "welcome", "v": 1, "heartbeat": 20, "relay": __version__}, "welcome")
    d = r.device("pipe-pi")
    check(d and d["online"] and d["slot"] == "B", "a device over a pipe is listed: %s" % d)
    c.send_text('{"t":"ping"}')
    status, recs = c.down(ack=1)
    check(status == 200 and [(k, q, json.loads(p)) for k, q, p in recs] == [(K_TEXT, 1, {"t": "pong"})], "pong: %s" % recs)
    c.close(1000)
    check(wait_until(lambda: not r.device("pipe-pi")["online"], 5), "a close record ends the device's connection")
    status, recs = c.down(ack=2)
    check(status == 200 and recs[0][0] == K_CLOSE and recs[0][1] == 2, "the relay answers the close: %s" % recs)
    check(c.down(ack=3)[0] == 410, "after its close was acknowledged the pipe is gone (410)")
    dev = PipeDevice(r, "pipe-pi2", name="Pipe Two")
    dev.send({"t": "ping"})
    check(dev.recv() == {"t": "pong"}, "ping -> pong over pipes")
    dev.close()
    log = r.log()
    check("device online: pipe-pi (Pipe Pi) from 127.0.0.1, version 2.1.0, slot B, transport http" in log,
          "the log names the transport")
    check(c.info["pipe"] not in log and dev.c.info["pipe"] not in log, "pipe ids are never logged in full")


@test("RLY-27")
def pipe_viewer_with_websocket_device(ctx):
    r = ctx["main"]
    dev = FakeDevice(r, "mixv-pi")
    v, d, opened = join(r, dev, viewer=pipe_viewer(r.url(""), "mixv-pi"))
    check(opened["addr"] == "127.0.0.1" and r.device("mixv-pi")["viewers"] == 1, "joined: %s" % opened)
    batch = [(K_TEXT, '{"t":"hello","v":1,"cn":"héllo"}'.encode()), (K_BINARY, os.urandom(1000)), (K_BINARY, b""),
             (K_TEXT, b"text-2"), (K_BINARY, os.urandom(200000))]
    v.send(*batch)
    for kind, payload in batch:
        check(d.recv_message() == (OP_OF[kind], payload), "viewer pipe -> device socket unchanged, in order")
    downs = [(wsmod.OP_BINARY, os.urandom(70000)), (wsmod.OP_TEXT, b"down-text"), (wsmod.OP_BINARY, b"\x00"),
             (wsmod.OP_BINARY, os.urandom(e2e.MAX_MESSAGE))]
    for op, payload in downs:
        d._send(op, payload)
    for op, payload in downs:
        check(v.recv_message() == (op, payload), "device socket -> viewer pipe unchanged, in order")
    v.close(4001, "viewer leaves")
    check(close_code(d) == 4001, "the viewer's close code reaches the device")
    check(pipe_close_code(v) == 4001, "the pipe gets the relay's answer to its close")
    check(wait_until(lambda: r.device("mixv-pi")["viewers"] == 0, 5), "the channel is gone")
    check("channel open: device mixv-pi, viewer 127.0.0.1, transport viewer http, device ws" in r.log(), "log")
    dev.close()


@test("RLY-27")
def pipe_device_with_websocket_viewer(ctx):
    r = ctx["main"]
    dev = PipeDevice(r, "mixd-pi")
    v, d, opened = join(r, dev)
    check(isinstance(d, PipeConn) and r.device("mixd-pi")["viewers"] == 1, "joined to the device's data pipe")
    ups = [os.urandom(300000), b"\x01\x02", os.urandom(e2e.MAX_MESSAGE)]
    for payload in ups:
        v.send_binary(payload)
    v.send_text("up-text")
    for payload in ups:
        check(d.recv_message() == (wsmod.OP_BINARY, payload), "viewer socket -> device pipe unchanged, in order")
    check(d.recv_message() == (wsmod.OP_TEXT, b"up-text"), "text stays text")
    d.send((K_BINARY, b"a" * 100), (K_TEXT, b"b"), (K_BINARY, os.urandom(150000)))
    check(v.recv_message() == (wsmod.OP_BINARY, b"a" * 100) and v.recv_message() == (wsmod.OP_TEXT, b"b")
          and len(v.recv_message()[1]) == 150000, "device pipe -> viewer socket unchanged, in order")
    d.close(4002, "device leaves")
    check(close_code(v) == 4002, "the device's close code reaches the viewer")
    check(wait_until(lambda: r.device("mixd-pi")["viewers"] == 0, 5), "the channel is gone")
    v, d, _ = join(r, dev, viewer=pipe_viewer(r.url(""), "mixd-pi"))
    v.send_text("both-pipes")
    check(d.recv_message() == (wsmod.OP_TEXT, b"both-pipes"), "viewer pipe -> device pipe")
    d.send_binary(b"back")
    check(v.recv_message() == (wsmod.OP_BINARY, b"back"), "device pipe -> viewer pipe")
    check("channel open: device mixd-pi, viewer 127.0.0.1, transport http" in r.log(), "log: both over http")
    v.close(1000)
    check(pipe_close_code(d) == 1000, "pipe to pipe close")
    dev.close()
    check(wait_until(lambda: not r.device("mixd-pi")["online"], 5), "the device goes offline")


@test("RLY-27")
def pipe_up_retries_gaps_and_bad_bodies(ctx):
    r = ctx["main"]
    dev = FakeDevice(r, "seq-pi")
    v, d, _ = join(r, dev, viewer=pipe_viewer(r.url(""), "seq-pi"))
    check(v.up((K_TEXT, b"m0"), (K_TEXT, b"m1"), seq=0) == (200, {"next": 2}), "two records applied")
    check(v.up((K_TEXT, b"m0"), (K_TEXT, b"m1"), seq=0) == (200, {"next": 2}), "a retried request is harmless")
    check(v.up((K_TEXT, b"m1"), (K_BINARY, b"m2"), seq=1) == (200, {"next": 3}), "only the new record is applied")
    check(v.up((K_TEXT, b"m9"), seq=5) == (409, {"next": 3}), "a gap -> 409 with the next expected index")
    got = [d.recv_message() for _ in range(3)]
    check(got == [(wsmod.OP_TEXT, b"m0"), (wsmod.OP_TEXT, b"m1"), (wsmod.OP_BINARY, b"m2")], "each once, in order: %s" % got)
    d.sock.settimeout(0.5)
    try:
        d.recv_message()
        raise AssertionError("a record was forwarded twice")
    except socket.timeout:
        pass
    d.sock.settimeout(5)
    for body, what in ((b"\x01\x00\x00", "a cut header"), (b"\x01\x00\x00\x00\x09abc", "a cut record"),
                       (b"\x05\x00\x00\x00\x00", "an unknown kind")):
        status, _, answer = http(v.url + "/up?seq=3", "POST", body)
        check(status == 400 and b"bad up body" in answer, "%s -> 400: %d %s" % (what, status, answer))
    check(http(v.url + "/up", "POST", up_body((K_TEXT, b"x")))[0] == 400, "seq is required")
    check(http(v.url + "/down")[0] == 400, "ack is required")
    check(http(v.url + "/down?ack=99")[0] == 400, "an ack ahead of the messages sent -> 400")
    check(http(v.url + "/up?seq=3")[0] == 405 and http(v.url + "/down?ack=0", "POST", b"")[0] == 405, "methods")
    limit = e2e.PIPE_MAX_UP + relay_pipe.PIPE_SLACK
    with socket.create_connection(("127.0.0.1", r.port), timeout=5) as s:
        s.sendall(("POST %s/up?seq=3 HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n"
                   % (v.url.split(str(r.port), 1)[1], limit + 1)).encode())
        check(s.recv(4096).split(b" ")[1] == b"413", "a body over PIPE_MAX_UP -> 413")
    full = os.urandom(e2e.PIPE_MAX_UP - 5)
    check(v.up((K_BINARY, full), seq=3) == (200, {"next": 4}), "a body of exactly PIPE_MAX_UP is taken")
    check(d.recv_message() == (wsmod.OP_BINARY, full), "and forwarded")
    v.up((K_TEXT, b"\xc3\x28"), seq=4)
    check(close_code(d) == 1007, "text that is not UTF-8 closes the channel with 1007, like a WebSocket")
    check(pipe_close_code(v) == 1007, "the pipe gets the 1007 close")
    check(http(r.url("/api/pipe/AAAAAAAAAAAAAAAAAAAAAA/down?ack=0"))[0] == 410, "an unknown pipe -> 410")
    check(http(r.url("/api/pipe/AAAAAAAAAAAAAAAAAAAAAA/up?seq=0"), "POST", b"")[0] == 410, "up to an unknown pipe -> 410")
    dev.close()


@test("RLY-27")
def pipe_acks_polls_idle_and_close(ctx):
    r = Relay(ctx, RELAY_PIPE_POLL="1.5", RELAY_PIPE_IDLE="3")
    base = r.url("")
    dev = FakeDevice(r, "ack-pi")
    v = PipeConn(base, "/api/connect/ack-pi", poll=False)
    check(v.info["poll"] == 1.5, "open names the poll time: %s" % v.info)
    d = dev.data(dev.recv()["sid"])
    check(recv_json(d) == {"t": "linked"}, "linked")
    check(v.down(0)[1] == [(K_TEXT, 0, b'{"t":"relay","ok":true}')], "relay ok is down message 0")
    for text in ("a", "b", "c"):
        d.send_text(text)
    got = []
    while len(got) < 3:
        status, recs = v.down(1)
        got = [(q, p) for _, q, p in recs]
    check(got == [(1, b"a"), (2, b"b"), (3, b"c")], "seq numbers count down messages: %s" % got)
    check([(q, p) for _, q, p in v.down(1)[1]] == got, "messages not acknowledged are sent again")
    check([(q, p) for _, q, p in v.down(3)[1]] == [(3, b"c")], "ack forgets the messages before it")
    t0 = time.monotonic()
    status, _ = v.down(4)
    check(status == 204 and 1.2 < time.monotonic() - t0 < 4, "nothing to send: 204 after the poll time (%d, %.1f s)"
          % (status, time.monotonic() - t0))
    first = {}

    def older(ack):
        first["answer"] = v.down(ack)
        first["t"] = time.monotonic() - t0
    t0 = time.monotonic()
    th = threading.Thread(target=older, args=(4,))
    th.start()
    time.sleep(0.3)
    d.send_text("m4")
    th.join(5)
    check(first["answer"] == (200, [(K_TEXT, 4, b"m4")]) and first["t"] < 1.0,
          "a waiting down request answers as soon as a message is queued: %s" % first)
    t0 = time.monotonic()
    th = threading.Thread(target=older, args=(5,))
    th.start()
    time.sleep(0.3)
    threading.Timer(0.5, lambda: d.send_text("m5")).start()
    status, recs = v.down(5)
    th.join(5)
    check(first["answer"][0] == 204 and first["t"] < 0.8, "a newer down request ends the older one with 204: %s" % first)
    check((status, recs) == (200, [(K_TEXT, 5, b"m5")]), "the newer one gets the message: %s %s" % (status, recs))
    big = [os.urandom(700000) for _ in range(3)]
    sender = threading.Thread(target=lambda: [d.send_binary(b) for b in big] + [d.send_binary(os.urandom(1 << 20))
                                                                                  for _ in range(4)])
    sender.start()
    ack, sizes, got = 6, [], []
    while len(got) < 7:
        status, recs = v.down(ack)
        if status == 200:
            sizes.append(sum(9 + len(p) for _, _, p in recs))
            got += [p for _, _, p in recs]
            ack = recs[-1][1] + 1
    sender.join(10)
    check(got[:3] == big and all(len(p) == 1 << 20 for p in got[3:]), "7 MiB through the down buffer, in order")
    check(all(n <= (1 << 20) + 9 for n in sizes) and len(sizes) == 7, "a down answer is at most 1 MiB, at least one "
          "whole message: %s" % sizes)
    v.close(1000)
    check(close_code(d) == 1000, "the pipe's close reaches the device")
    status, recs = v.down(ack)
    check(status == 200 and recs[-1][0] == K_CLOSE, "the relay's close record")
    check(v.up((K_TEXT, b"late"), seq=v.seq)[0] == 200, "up after the close: taken but dropped (the close is not "
          "acknowledged yet)")
    check(v.down(recs[-1][1] + 1)[0] == 410 and v.up((K_TEXT, b"x"), seq=0)[0] == 410, "410 once the close is acknowledged")
    # idle: a viewer pipe without requests is dropped; the device's side is closed
    w, d2, _ = join(r, dev, viewer=pipe_viewer(base, "ack-pi"))
    w.stopped = True                # its last down request ends within the poll time, then nothing
    d2.sock.settimeout(10)
    t0 = time.monotonic()
    check(close_code(d2) == 1001, "a dropped pipe closes the other side like a lost connection")
    check(3 < time.monotonic() - t0 < 8, "dropped after the idle time: %.1f s" % (time.monotonic() - t0))
    check(wait_until(lambda: http(w.url + "/down?ack=%d" % w.ack, timeout=5)[0] == 410, 3), "the dropped pipe -> 410")
    check(wait_until(lambda: re.search(r"pipe [A-Za-z0-9_-]{6}\.\.\. for /api/connect/ack-pi from 127.0.0.1 dropped: "
                                       r"no request for 3 s", r.log()), 3), "the drop is logged")
    dev.close()
    quiet = PipeDevice(r, "quiet-pipe")
    quiet.c.stopped = True
    check(wait_until(lambda: not r.device("quiet-pipe")["online"], 10), "a device whose pipe goes quiet goes offline")
    r.stop()


@test("RLY-27")
def pipe_open_refused_like_websockets(ctx):
    r = Relay(ctx, RELAY_PASSWORD="pw-pipe")
    base = r.url("")
    bearer = {"Authorization": "Bearer pw-pipe"}
    check(pipe_open(base, "/api/connect/some-pi")[0] == 401, "no password -> 401")
    check(pipe_open(base, "/api/connect/some-pi", {"Authorization": "Bearer nope"})[0] == 401, "wrong password -> 401")
    status, _, body = pipe_open(base, "/api/connect/some-pi", dict(bearer, Origin="https://evil.example"))
    check(status == 403 and b"other web sites" in body, "another web site -> 403: %d" % status)
    check(pipe_open(base, "/api/connect/Bad_Id", bearer)[0] == 404, "an invalid device id -> 404")
    v = PipeConn(base, "/api/connect/nobody-here", dict(bearer, Origin="http://127.0.0.1:%d" % r.port))
    check(recv_json(v) == {"t": "relay", "ok": False, "error": "no device with id nobody-here"},
          "an unknown device: refused inside the pipe, like the WebSocket")
    expect_closed(v, "refused viewer pipe")
    check(pipe_open(base, "/api/elsewhere")[0] == 404, "a route that is not a WebSocket route -> 404")
    check(http(base + "/api/pipe/open", "POST", b"not json")[0] == 400, "a broken open body -> 400")
    bad = PipeDevice(r, "tok-pipe", token="wrong", welcome=False)
    check(bad.recv() == {"t": "error", "error": "wrong join token"}, "a wrong join token, inside the pipe")
    expect_closed(bad.c, "refused device pipe")
    check(bad.c.close_code == 1008, "closed with 1008 like the WebSocket")
    r.stop()
    r = Relay(ctx, RELAY_RATE_LIMIT="3/30")
    for _ in range(3):
        check(pipe_open(r.url(""), "/api/agent")[0] == 200, "within the rate limit")
    status, h, _ = pipe_open(r.url(""), "/api/agent")
    check(status == 429 and h.get("Retry-After"), "the 4th new connection in the window -> 429")
    try:
        wsconnect(r.ws_url("/api/agent"))
        raise AssertionError("pipes and WebSockets share the rate limit")
    except ConnectionError as e:
        check("429" in str(e), "-> 429: %s" % e)
    check(pipe_open(r.url(""), "/api/agent/data")[0] == 200, "data connections are not rate limited")
    r.stop()
    r = Relay(ctx)
    codes = [pipe_open(r.url(""), "/api/agent/data")[0] for _ in range(relay_pipe.PIPES_PER_ADDRESS + 1)]
    check(codes == [200] * relay_pipe.PIPES_PER_ADDRESS + [429], "at most %d pipes per address" % relay_pipe.PIPES_PER_ADDRESS)
    r.stop()


@test("RLY-27")
def pipe_device_replaced_and_shutdown_bye(ctx):
    r = Relay(ctx)
    first = PipeDevice(r, "bye-pipe")
    second = PipeDevice(r, "bye-pipe")
    bye = first.recv()
    check(bye.get("t") == "bye" and "replaced" in bye.get("reason", ""), "a replaced pipe device gets bye: %s" % bye)
    expect_closed(first.c, "replaced device pipe")
    time.sleep(0.3)                 # the second's next down request is waiting (not still connecting)
    r.proc.send_signal(signal.SIGTERM)
    check(second.recv() == {"t": "bye", "reason": "the device manager is shutting down"}, "bye on shutdown over a pipe")
    check(r.proc.wait(5) == 0, "exits cleanly")
    r.stop()


@test("RLY-27")
def pipes_through_nginx_without_websockets(ctx):
    r = Relay(ctx, RELAY_TRUST_PROXY="1", RELAY_PIPE_POLL="3")
    proxy = NginxProxy(r.port)
    try:
        url = proxy.start()
    except NginxUnavailable as e:
        print("      SKIP pipes_through_nginx_without_websockets: %s" % e, flush=True)
        r.stop()
        return
    try:
        base = url + BASE
        check(http(base + "/api/health")[0] == 200, "the relay answers through nginx")
        try:
            wsconnect("ws://127.0.0.1:%d%s/api/agent" % (proxy.port, BASE))
            raise AssertionError("a WebSocket must not pass this nginx")
        except ConnectionError as e:
            check("426" in str(e), "WebSocket through nginx -> the relay's 426: %s" % e)
        dev = PipeDevice(r, "nginx-pi", base=base)
        v, d, opened = join(r, dev, viewer=pipe_viewer(base, "nginx-pi", {"Origin": url}))
        ups = [(K_TEXT, b'{"t":"hello"}'), (K_BINARY, os.urandom(250000))]
        v.send(*ups)
        for kind, payload in ups:
            check(d.recv_message() == (OP_OF[kind], payload), "viewer -> device through nginx, unchanged")
        downs = [(K_BINARY, os.urandom(200000)), (K_TEXT, b"down")]
        d.send(*downs)
        for kind, payload in downs:
            check(v.recv_message() == (OP_OF[kind], payload), "device -> viewer through nginx, unchanged")
        check(http(v.url + "/up?seq=%d" % v.seq, "POST", up_body((K_BINARY, os.urandom(1 << 20))))[0] == 413,
              "an up request over 1 MiB is refused (by nginx's client_max_body_size)")
        direct = FakeDevice(r, "direct-pi")           # a device with WebSockets, a viewer behind nginx
        v2, d2, _ = join(r, direct, viewer=pipe_viewer(base, "direct-pi", {"Origin": url}))
        big = os.urandom(e2e.MAX_MESSAGE)
        d2.send_binary(big)
        d2.send_text("after big")
        check(v2.recv_message() == (wsmod.OP_BINARY, big) and v2.recv_message() == (wsmod.OP_TEXT, b"after big"),
              "a 1 MiB message down through nginx")
        v2.close()
        direct.close()
        time.sleep(4)              # longer than the poll time: down requests end with 204 and are made again
        v.send_text("after a quiet while")
        check(d.recv_message() == (wsmod.OP_TEXT, b"after a quiet while"), "still joined after 204 answers")
        v.close(4003)
        check(pipe_close_code(d) == 4003, "the close code through nginx")
        dev.close()
        check(wait_until(lambda: not r.device("nginx-pi")["online"], 5), "offline after its close")
        check("device online: nginx-pi (Lab Pi) from 127.0.0.1" in r.log(), "nginx's X-Forwarded-For is used")
        check(" 426 " in proxy.logs() and " 204 " in proxy.logs(), "nginx saw the 426 and the 204 answers")
    finally:
        proxy.stop()
        r.stop()


def setup():
    tmp = tempfile.mkdtemp(prefix="relay-test-")
    static = os.path.join(tmp, "static")
    for name, text in STATIC_FILES.items():
        os.makedirs(os.path.dirname(os.path.join(static, name)), exist_ok=True)
        with open(os.path.join(static, name), "w") as f:
            f.write(text)
    with open(os.path.join(tmp, "server.py"), "w") as f:
        f.write("outside the static directory")
    ctx = {"tmp": tmp, "static": static, "relays": []}
    ctx["main"] = Relay(ctx)
    return ctx


def teardown(ctx):
    for r in ctx["relays"]:
        r.stop()
    shutil.rmtree(ctx["tmp"], ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run(setup, teardown, description=__doc__))
