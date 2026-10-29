#!/usr/bin/env python3
"""The device manager (relay server): routes, registry, forwarding, limits, admin (RLY-21..26).

    python3 relay/tests/test_relay_server.py [-k NAME]

Starts the real server (python3 -m arstro_relay) as a subprocess on a free port with a
temporary data directory and plays fake devices and viewers with arstro_remote.web.ws.
Pages come from a temporary static directory, so the tests do not depend on the viewer's files.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
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
from arstro_relay import server as relay_server  # noqa: E402
from arstro_remote.relay import e2e  # noqa: E402
from arstro_remote.web import ws as wsmod  # noqa: E402

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


def http(url, method="GET", body=None, headers=None):
    """-> (status, headers, body bytes); never follows redirects."""
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if isinstance(body, dict):
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=5) as r:
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
        self.c = wsconnect(relay.ws_url("/api/agent"))
        hello = {"t": "hello", "v": 1, "id": dev_id, "name": name, "secret": self.secret,
                 "token": relay.token() if token is None else token, "version": "2.1.0", "hostname": "pi5",
                 "platform": "linux-aarch64", "slot": "A", "web": 8080}
        hello.update(extra)
        self.c.send_text(json.dumps(hello))
        if welcome:
            self.welcome = self.recv()
            check(self.welcome.get("t") == "welcome", "%s: expected welcome, got %s" % (dev_id, self.welcome))

    def recv(self):
        return recv_json(self.c)

    def send(self, obj):
        self.c.send_text(json.dumps(obj))

    def data(self, sid, secret=None, dev_id=None):
        d = wsconnect(self.relay.ws_url("/api/agent/data"))
        d.send_text(json.dumps({"t": "data", "v": 1, "id": dev_id or self.id, "secret": secret or self.secret,
                                "sid": sid}))
        return d

    def close(self):
        self.c.close()


def join(relay, dev, headers=None):
    """Viewer -> relay -> device: a joined channel -> (viewer socket, device data socket)."""
    v = wsconnect(relay.ws_url("/api/connect/" + dev.id), headers=headers)
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


# --------------------------------------------------------------------- tests
@test("RLY-21")
def protocol_constants_match_e2e(ctx):
    check(relay_server.MAX_MESSAGE == e2e.MAX_MESSAGE, "MAX_MESSAGE differs from e2e.py")
    check(set(relay_server.MESSAGES) == set(e2e.RELAY_MESSAGES),
          "relay messages %s != e2e.RELAY_MESSAGES %s" % (sorted(relay_server.MESSAGES), sorted(e2e.RELAY_MESSAGES)))
    check(relay_server.PROTOCOL == e2e.VERSION, "protocol version differs")
    check(relay_server.CHANNELS_PER_DEVICE == 32 and relay_server.MAX_DEVICES == 1000, "limits of PROTOCOL.md section 6")


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
    v, d, opened = join(r, dev, headers=dict(jar, **{"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}))
    check(opened["addr"] == "203.0.113.7", "RELAY_TRUST_PROXY: the first X-Forwarded-For entry: %s" % opened)
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
    for name in ("client.py", "e2e.py", "ws.py"):
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
    for name in ("arstro_relay_client.py", "e2e.py", "ws.py"):
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
    for name, path in (("e2e.py", "relay/e2e.py"), ("ws.py", "web/ws.py"), ("arstro_relay_client.py", "relay/client.py")):
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
