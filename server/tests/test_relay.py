"""Internet access, the device side (module `relay`, RLY-01..08, RLY-27).

Unit level: a throwaway instance (identity, settings, topic, validation, key rotation, a
disabled module) and the device's end of a viewer channel over a socket pair (handshake,
streams, header rules) - no device manager needed. Integration: the real device manager
(relay/arstro_relay) as a subprocess on a free port, the instance advertising to it, and
viewers using the standalone client (server/arstro_remote/relay/client.py): online in the
list, ready, HTTP / WebSocket / TCP / ports, allow-list, kick, rotate, wrong keys. The HTTP
transport (pipe.py, RLY-27): a device and viewers forced onto pipes, a down poll broken again
and again, and the automatic fallback behind a proxy that does not pass WebSockets (a small
Python one, and nginx 1.14 in Docker when Docker is there - relay/tests/nginx_proxy.py).

    python3 server/tests/test_relay.py [-k NAME]
"""

import contextlib
import hashlib
import http.client
import http.server
import json
import logging
import os
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from harness import check, run, test, wait_until

from instance import Instance, free_port

from arstro_remote.relay import agent as ag
from arstro_remote.relay import client, e2e, pipe, service
from arstro_remote.web import ws

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.getLogger("arstro").addHandler(logging.NullHandler())   # the unit-level Agent logs here
BASE = "/arstro/internal/devices"


# ------------------------------------------------------------------ local test servers
class EchoTCP:
    """Echoes bytes; closes its side after the client's half-close."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _serve(c):
        with c:
            while True:
                d = c.recv(65536)
                if not d:
                    break
                c.sendall(d)


class HeaderEcho(http.server.BaseHTTPRequestHandler):
    """Answers with the request it got: {method, path, headers [[k, v]], body length, body sha}."""
    protocol_version = "HTTP/1.1"

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path.startswith("/big/"):
            size = int(self.path.split("/")[2])
            out = bytes(range(256)) * (size // 256)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Keep-Alive", "timeout=5")
            self.end_headers()
            self.wfile.write(out)
            return
        out = json.dumps({"method": self.command, "path": self.path, "headers": [[k, v] for k, v in self.headers.items()],
                          "body": len(body), "text": body[:64].decode("latin-1"),
                          "sha": hashlib.sha256(body).hexdigest()}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Set-Cookie", "a=1")
        self.send_header("Set-Cookie", "b=2")
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = _answer

    def log_message(self, *a):
        pass


def header_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HeaderEcho)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------------ a viewer over a socket pair
class PairViewer:
    """The viewer's end of a channel without a device manager (e2e.py by hand)."""

    def __init__(self, sock, key):
        self.c = ws.WSConnection(sock, client=True)
        cn, hello = e2e.viewer_hello()
        self.c.send_text(json.dumps(hello))
        op, data = self.c.recv_message()
        self.sec, proof = e2e.viewer_check(e2e.parse_key(key), cn, json.loads(data))
        self.c.send_text(json.dumps(proof))
        op, data = self.c.recv_message()
        if op == ws.OP_TEXT:
            raise e2e.WrongKey(json.loads(data).get("error"))
        kind, sid, _f, payload = self.sec.open(data)
        self.ready = e2e.decode_json(payload)
        self.lock = threading.Lock()

    def send(self, kind, sid, payload, flags=0):
        with self.lock:
            self.c.send_binary(self.sec.seal(kind, sid, payload, flags))

    def json(self, sid, obj):
        self.send(e2e.K_JSON, sid, json.dumps(obj).encode())

    def next(self, sid):
        """Next frame of stream sid (acknowledging DATA at once)."""
        while True:
            op, data = self.c.recv_message()
            kind, s, flags, payload = self.sec.open(data)
            if kind == e2e.K_DATA and payload:
                self.json(s, {"t": "ack", "n": len(payload)})
            if s == sid:
                return kind, flags, (e2e.decode_json(payload) if kind == e2e.K_JSON else payload)

    def http(self, sid, opener, body=None):
        self.json(sid, dict(opener, t="http", body=body is not None))
        if body is not None:
            for i in range(0, len(body), e2e.CHUNK):
                self.send(e2e.K_DATA, sid, body[i:i + e2e.CHUNK])
            self.json(sid, {"t": "end"})
        kind, _f, head = self.next(sid)
        check(kind == e2e.K_JSON and head["t"] == "res", head)
        out = bytearray()
        while True:
            kind, _f, x = self.next(sid)
            if kind == e2e.K_DATA:
                out += x
            else:
                check(x["t"] == "end", x)
                return head, bytes(out)


def pair_channel(key, ports=None):
    """A device Channel on one end of a socket pair -> (agent, channel, device socket, viewer socket)."""
    a = ag.Agent()
    a.configure({"enabled": False, "id": "unit-test", "name": "Unit test", "hostname": "unit", "version": "0",
                 "slot": "t", "ports": ports, "key_raw": e2e.parse_key(key)}, reconnect=False)
    ch = ag.Channel(a, 1, "sid-1", "203.0.113.9")
    a.channels[1] = ch
    d, v = socket.socketpair()
    threading.Thread(target=ch.run, args=(a.cfg, ws.WSConnection(d)), daemon=True).start()
    return a, ch, v


# ------------------------------------------------------------------ helpers
def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def cfg_dir(t):
    return os.path.join(t.dir, "cfg", "arstro-remote-t")


def identity(t):
    with open(os.path.join(cfg_dir(t), "relay_identity.json")) as f:
        return json.load(f)


def relay_status(t):
    return t.c.call("relay.status")


def connect(ctx, key=None):
    return client.Relay(ctx.relay_url, timeout=15).connect(ctx.device_id, key or identity(ctx.t)["key"])


def devices(ctx):
    with urllib.request.urlopen(ctx.relay_url + "/api/devices", timeout=5) as r:
        return json.loads(r.read())["devices"]


def frames(data):
    """Arstro protocol frames in a binary WebSocket message -> [json]."""
    out, i = [], 0
    while i + 5 <= len(data):
        t, n = struct.unpack_from(">BI", data, i)
        if t == 1:
            out.append(json.loads(data[i + 5:i + 5 + n]))
        i += 5 + n
    return out


# ================================================================== unit level
@test("RLY-01", "RLY-02")
def off_until_configured_identity_is_private(ctx):
    t = ctx.t
    st = (t.hello.get("state") or {}).get("relay")
    check(st and st["state"] == "off" and st["enabled"] is False and st["token_set"] is False, st)
    check(st["url"] is None and st["viewers"] == [] and st["ports"] is None, st)
    path = os.path.join(cfg_dir(t), "relay_identity.json")
    check(os.path.exists(path) and mode(path) == 0o600, "identity file missing or not 0600")
    ident = identity(t)
    check(re.match(r"^[a-z0-9][a-z0-9-]{2,39}$", ident["id"]) and ident["id"] == st["id"], ident["id"])
    check(len(ident["secret"]) >= 32 and len(e2e.parse_key(ident["key"])) == 32, "weak identity")
    check(st["key_id"] == e2e.key_id(e2e.parse_key(ident["key"])), st["key_id"])
    check(not os.path.exists(os.path.join(cfg_dir(t), "relay.json")), "relay.json written before any setting")
    mods = {m["name"]: m for m in t.hello["modules"]}
    check(mods["relay"]["state"] == "enabled" and mods["relay"]["title"] == "Internet access", mods["relay"])
    r = t.c.call("relay.key")
    check(r["key"] == ident["key"] and r["key_id"] == st["key_id"], "relay.key")


@test("RLY-08")
def settings_are_validated(ctx):
    t = ctx.t
    bad = [({"url": "ftp://example.com/x"}, "https://"), ({"url": "https://"}, "https://"),
           ({"url": "https://user:pw@example.com/x"}, "user name"), ({"url": "https://example.com/x?a=1"}, "query"),
           ({"token": ""}, "token"), ({"token": "a b"}, "token"), ({"name": "x" * 65}, "1-64"),
           ({"name": "bad\nname"}, "1-64"), ({"ports": [0]}, "port"), ({"ports": [70000]}, "port"),
           ({"ports": ["x"]}, "port"), ({"ports": [True]}, "port"), ({"ports": []}, "at least one"),
           ({"enabled": "yes"}, "true or false"), ({"enabled": True}, "join token"), ({}, "nothing to change")]
    for params, words in bad:
        r = t.c.request("relay.set", **params)
        check(r["ok"] is False and words in r["error"], (params, r))
    st = t.c.call("relay.set", name="  My box ", ports="8080, 22,8080")
    check(st["name"] == "My box" and st["ports"] == [22, 8080] and st["state"] == "off", st)
    path = os.path.join(cfg_dir(t), "relay.json")
    check(mode(path) == 0o600, "relay.json not 0600")
    check(t.c.wait_state("relay", lambda s: s["ports"] == [22, 8080]), "topic not updated")
    st = t.c.call("relay.set", ports=None, name=None)
    check(st["ports"] is None and st["name"] != "My box", st)


@test("RLY-08")
def settings_default_from_config_json(ctx):
    d = tempfile.mkdtemp(prefix="arstro-relay-cfg-")
    try:
        cfg = {"relay": {"url": "https://example.com/arstro/internal/devices/", "token": "t0k3n", "enabled": True,
                         "ports": [8080], "name": "Lab"}}
        s = service.load_settings(cfg, d)
        check(s == {"url": "https://example.com/arstro/internal/devices", "token": "t0k3n", "name": "Lab",
                    "enabled": True, "ports": [8080]}, s)
        service.save_settings(dict(s, enabled=False, ports=None), d)
        check(mode(os.path.join(d, "relay.json")) == 0o600, "relay.json mode")
        s2 = service.load_settings(cfg, d)
        check(s2["enabled"] is False and s2["ports"] is None, "relay.json must win over config.json")
        one = service.load_identity(d)
        check(service.load_identity(d) == one and mode(service.identity_path(d)) == 0o600, "identity not stable")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test("RLY-01", "MOD-01")
def ops_refused_when_the_module_is_disabled(ctx):
    b = Instance(["system"], app="test-relay-off")
    try:
        b.start()
        r = b.c.request("relay.status")
        check(r["ok"] is False and "Internet access module is not enabled" in r["error"], r)
        check("relay" not in (b.hello.get("state") or {}), "topic published by a disabled module")
        check(not os.path.exists(os.path.join(b.dir, "cfg", "arstro-remote-t", "relay_identity.json")),
              "a disabled module made an identity")
    finally:
        b.stop()


@test("RLY-03", "RLY-04")
def device_channel_handshake_streams_and_header_rules(ctx):
    key = e2e.new_key()
    a, ch, v = pair_channel(key)
    pv = PairViewer(v, key)
    check(pv.ready["t"] == "ready" and pv.ready["id"] == "unit-test" and pv.ready["ports"] is None, pv.ready)
    check(wait_until(lambda: a.viewers() and a.viewers()[0]["addr"] == "203.0.113.9", 3), a.viewers())
    pv.json(0, {"t": "ping"})
    check(pv.next(0)[2] == {"t": "pong"}, "no pong")
    # http: header rules, a body without Content-Length, several Set-Cookie
    hs = ctx.headers
    port = hs.server_address[1]
    head, body = pv.http(1, {"method": "POST", "port": port, "path": "/echo?x=1",
                             "headers": [["Host", "evil.example"], ["Origin", "https://relay.example"],
                                         ["Accept-Encoding", "gzip, br"], ["Connection", "keep-alive, X-Hop"],
                                         ["X-Hop", "1"], ["Keep-Alive", "300"], ["TE", "trailers"],
                                         ["Proxy-Authorization", "x"], ["X-Forwarded-For", "198.51.100.1"],
                                         ["Content-Type", "text/plain"], ["X-Keep", "yes"]]}, body=b"hello" * 1000)
    check(head["status"] == 201 and [k for k, _ in head["headers"]].count("Set-Cookie") == 2, head)
    got = json.loads(body)
    hdr = {k.lower(): v for k, v in got["headers"]}
    check(got["method"] == "POST" and got["path"] == "/echo?x=1" and got["body"] == 5000, got)
    check(hdr["host"] == "127.0.0.1:%d" % port and hdr["origin"] == "http://127.0.0.1:%d" % port, hdr)
    check(hdr["accept-encoding"] == "identity" and hdr["x-forwarded-for"] == "203.0.113.9", hdr)
    check(hdr["content-length"] == "5000" and hdr["x-keep"] == "yes", hdr)
    for gone in ("x-hop", "keep-alive", "te", "proxy-authorization", "transfer-encoding"):
        check(gone not in hdr, "%s passed through" % gone)
    check(hdr.get("connection") in (None, "close") or "x-hop" not in hdr.get("connection", ""), hdr)
    # a response bigger than the flow-control window
    head, body = pv.http(2, {"method": "GET", "port": port, "path": "/big/%d" % (3 * e2e.WINDOW), "headers": []})
    check(head["status"] == 200 and body == bytes(range(256)) * (3 * e2e.WINDOW // 256), "big body differs")
    check(not any(k.lower() == "keep-alive" for k, _ in head["headers"]), "response hop-by-hop header kept")
    # errors
    pv.json(3, {"t": "http", "method": "GET", "port": free_port(), "path": "/", "headers": [], "body": False})
    check("nothing listens" in pv.next(3)[2].get("error", ""), "closed port")
    pv.json(4, {"t": "tcp", "port": 0})
    check("1-65535" in pv.next(4)[2].get("error", ""), "port 0")
    pv.json(5, {"t": "nope"})
    check("unknown stream" in pv.next(5)[2].get("error", ""), "unknown stream")
    pv.c.close()
    check(wait_until(lambda: ch.closed and not a.viewers(), 5), "channel not closed with its connection")

    # wrong key, an honest viewer: it rejects the device's proof and goes away - counted
    a2, ch2, v2 = pair_channel(key)
    try:
        PairViewer(v2, e2e.new_key())
        check(False, "a wrong key was accepted")
    except e2e.WrongKey:
        v2.close()
    check(wait_until(lambda: a2.wrong_total == 1 and ch2.closed, 5), "wrong key not counted")
    # a viewer that sends a wrong proof anyway: the text error "wrong key", counted, closed
    a3, ch3, v3 = pair_channel(key)
    c = ws.WSConnection(v3, client=True)
    _cn, hello = e2e.viewer_hello()
    c.send_text(json.dumps(hello))
    check(json.loads(c.recv_message()[1])["t"] == "hello", "no device hello")
    c.send_text(json.dumps({"t": "proof", "proof": e2e.b64e(os.urandom(32))}))
    op, data = c.recv_message()
    check(op == ws.OP_TEXT and json.loads(data) == {"t": "error", "error": "wrong key"}, data)
    check(wait_until(lambda: a3.wrong_total == 1 and ch3.closed, 5), "wrong proof not counted")
    c.close()


@test("RLY-04")
def device_channel_allow_list_and_flow_control(ctx):
    key = e2e.new_key()
    echo = ctx.echo.port
    a, ch, v = pair_channel(key, ports=[echo])
    pv = PairViewer(v, key)
    check(pv.ready["ports"] == [echo], pv.ready)
    pv.json(1, {"t": "tcp", "port": ctx.headers.server_address[1]})
    check(pv.next(1)[2].get("error") == "port %d is not allowed on this device" % ctx.headers.server_address[1],
          "allow-list")
    pv.json(2, {"t": "tcp", "port": echo})
    check(pv.next(2)[2] == {"t": "open"}, "tcp open")
    blob = os.urandom(3 * e2e.WINDOW)
    acked = [0]

    def reader(out):
        while len(out) < len(blob):
            kind, _f, x = pv.next(2)
            if kind == e2e.K_DATA:
                out += x
            elif x.get("t") == "ack":
                acked[0] += x["n"]
    got = bytearray()
    th = threading.Thread(target=reader, args=(got,), daemon=True)
    th.start()
    for i in range(0, len(blob), e2e.CHUNK):         # this viewer ignores the window: the echo keeps up
        pv.send(e2e.K_DATA, 2, blob[i:i + e2e.CHUNK])
    th.join(20)
    check(bytes(got) == blob, "tcp echo differs (%d of %d bytes)" % (len(got), len(blob)))
    check(acked[0] >= len(blob) - e2e.WINDOW // 4, "the device did not acknowledge (%d)" % acked[0])
    pv.json(2, {"t": "end"})
    kind, _f, x = pv.next(2)
    check(x == {"t": "end"}, x)
    pv.c.close()


# ================================================================== with the device manager
@test("RLY-01", "RLY-08")
def device_goes_online_at_the_device_manager(ctx):
    t = ctx.t
    st = t.c.call("relay.set", url=ctx.relay_url + "/", token=ctx.join, name="Relay test", enabled=True)
    check(st["enabled"] and st["token_set"] and st["url"] == ctx.relay_url, st)
    check(t.c.wait_state("relay", lambda s: s["state"] == "online", 15), t.c.state.get("relay"))
    st = t.c.state["relay"]
    check(st["relay_version"] and st["error"] is None, st)
    ctx.device_id = st["id"]
    d = {x["id"]: x for x in devices(ctx)}.get(st["id"])
    check(d and d["online"] and d["name"] == "Relay test" and d["web"] == t.port and d["slot"] == "t", d)
    path = os.path.join(cfg_dir(t), "relay.json")
    with open(path) as f:
        saved = json.load(f)
    check(mode(path) == 0o600 and saved["token"] == ctx.join and saved["enabled"] is True, "relay.json")
    # a wrong join token: an error state with the device manager's reason, then back online
    t.c.call("relay.set", token="wrong-token")
    check(t.c.wait_state("relay", lambda s: s["state"] == "error" and "token" in (s["error"] or ""), 15),
          t.c.state.get("relay"))
    t.c.call("relay.set", token=ctx.join)
    check(t.c.wait_state("relay", lambda s: s["state"] == "online", 15), t.c.state.get("relay"))


@test("RLY-06", "RLY-08", "RLY-02")
def cli_manages_it_and_reaches_devices(ctx):
    t = ctx.t

    def cli(*args, ok=True, env=None):
        r = subprocess.run([sys.executable, "-m", "arstro_remote"] + list(args), cwd=os.path.join(ROOT, "server"),
                           env=dict(t.env, **(env or {})), capture_output=True, text=True, timeout=60)
        check((r.returncode == 0) == ok, "%s: rc %d %s %s" % (args, r.returncode, r.stdout[-300:], r.stderr[-300:]))
        return r.stdout + r.stderr if not ok else r.stdout
    st = json.loads(cli("--json", "relay", "status"))
    check(st["state"] == "online" and st["id"] == ctx.device_id, st)
    human = cli("relay", "status")
    check("online" in human and ctx.device_id in human and ctx.join not in human, human)
    st = json.loads(cli("relay", "set", "--ports", "8080,22", "--json"))
    check(st["ports"] == [22, 8080], st)
    check(json.loads(cli("relay", "set", "--ports", "any", "--json"))["ports"] is None, "ports any")
    check("--yes" in cli("relay", "rotate", ok=False), "rotate must ask for --yes")
    check("join token" in cli("relay", "set", "--token", "", ok=False), "an empty token must be refused")
    key = cli("relay", "key").strip()
    check(key == identity(t)["key"], "relay key")
    check(json.loads(cli("relay", "kick", "--json")) == {"closed": 0}, "kick")
    # the viewer side: the standalone client; without --relay this slot's own device manager
    devs = json.loads(cli("relay", "devices", "--json"))
    check(any(d["id"] == ctx.device_id and d["online"] for d in devs), devs)
    check("key ok" in cli("relay", "check", ctx.device_id, "--key", key, "--relay", ctx.relay_url), "check")
    out = cli("relay", "http", ctx.device_id, str(t.port), "/api/ping", env={"ARSTRO_RELAY_KEY": key})
    check('"version"' in out, out[:200])


@test("RLY-03", "RLY-05")
def viewer_handshake_gets_ready_and_is_listed(ctx):
    t = ctx.t
    ch = connect(ctx)
    try:
        info = ch.info
        check(info["t"] == "ready" and info["id"] == ctx.device_id and info["name"] == "Relay test", info)
        check(info["slot"] == "t" and info["ports"] is None and info["version"], info)
        check(t.c.wait_state("relay", lambda s: len(s["viewers"]) == 1, 5), t.c.state["relay"])
        v = t.c.state["relay"]["viewers"][0]
        check(v["addr"] == "127.0.0.1" and v["bytes_in"] > 0 and v["bytes_out"] > 0, v)
    finally:
        ch.close()
    check(t.c.wait_state("relay", lambda s: not s["viewers"], 10), t.c.state["relay"])
    lines = open(t.log, encoding="utf-8", errors="replace").read()
    check("viewer" in lines and "connected from 127.0.0.1" in lines and "tunnel up" in lines, "not logged")


@test("RLY-04")
def http_and_websocket_to_the_own_web_server(ctx):
    t = ctx.t
    ch = connect(ctx)
    try:
        st, _r, hdrs, body = ch.http("GET", t.port, "/api/ping")
        check(st == 200 and "version" in json.loads(body), body[:200])
        auth = {"Authorization": "Bearer " + t.token, "Content-Type": "application/json"}
        big = "x" * 300_000                                       # > WINDOW/4: acks both ways
        st, _r, _h, body = ch.http("POST", t.port, "/api/op/ping", auth, json.dumps({"t": big}).encode())
        r = json.loads(body)
        check(st == 200 and r["ok"] and r["data"]["echo"] == big, body[:200])
        st, _r, _h, body = ch.http("POST", t.port, "/api/op/ping", {"Content-Type": "application/json"}, b"{}")
        check(st == 401, "no password must be refused (%s)" % st)
        # WebSocket: the server's own /ws, one JSON op as a text message
        s = ch.stream({"t": "ws", "port": t.port, "path": "/ws", "protocols": [],
                       "headers": [["Authorization", "Bearer " + t.token], ["Origin", "https://relay.example"]]})
        ev = s.next(10)
        check(ev[0] == "json" and ev[1]["t"] == "open", ev)
        s.send_data(json.dumps({"op": "hello", "id": 7, "app": "relay-test"}).encode(), e2e.F_FIN | e2e.F_TEXT)
        reply = None
        end = time.monotonic() + 10
        while reply is None and time.monotonic() < end:
            ev = s.next(10)
            check(ev[0] == "data", ev)
            reply = next((m for m in frames(ev[2]) if m.get("id") == 7), None)
        check(reply and reply["ok"] and reply["data"]["slot"] == "t", reply)
        s.send_json({"t": "close", "code": 1000, "reason": "done"})
        s.close()
    finally:
        ch.close()


@test("RLY-04")
def tcp_ports_and_allow_list(ctx):
    t = ctx.t
    ch = connect(ctx)
    try:
        ports = {p["port"]: p for p in ch.ports()}
        check(t.port in ports and ctx.echo.port in ports, sorted(ports))
        check(ports[t.port]["proc"], "the instance's own process is not named: %s" % ports[t.port])
        s = ch.tcp(ctx.echo.port)
        blob = os.urandom(200_000)
        s.send_data(blob)
        got = bytearray()
        while len(got) < len(blob):
            ev = s.next(10)
            check(ev[0] == "data", ev)
            got += ev[2]
        check(bytes(got) == blob, "echo differs")
        s.send_json({"t": "end"})
        ev = s.next(10)
        check(ev[0] == "json" and ev[1]["t"] == "end", ev)
        s.close()
        try:
            ch.http("GET", free_port(), "/")
            check(False, "a closed port answered")
        except client.RelayError as e:
            check("nothing listens on port" in str(e), e)
    finally:
        ch.close()
    t.c.call("relay.set", ports=[t.port])
    try:
        ch = connect(ctx)
        check(ch.info["ports"] == [t.port], ch.info)
        try:
            ch.tcp(ctx.echo.port)
            check(False, "a port outside the allow-list was opened")
        except client.RelayError as e:
            check(str(e) == "port %d is not allowed on this device" % ctx.echo.port, e)
        check([p["port"] for p in ch.ports()] == [t.port], "ports must list only allowed ports")
        st, _r, _h, _b = ch.http("GET", t.port, "/api/ping")
        check(st == 200, st)
        ch.close()
    finally:
        t.c.call("relay.set", ports=None)


@test("RLY-05", "RLY-02")
def kick_and_rotate_disconnect_viewers(ctx):
    t = ctx.t
    ch = connect(ctx)
    check(t.c.wait_state("relay", lambda s: len(s["viewers"]) == 1, 5), t.c.state["relay"])
    r = t.c.call("relay.kick")
    check(r["closed"] == 1, r)
    check(wait_until(lambda: ch.closed, 10), "kicked channel still open")
    check(t.c.wait_state("relay", lambda s: not s["viewers"], 5), t.c.state["relay"])
    old = identity(t)
    ch = connect(ctx)
    check(t.c.wait_state("relay", lambda s: len(s["viewers"]) == 1, 5), t.c.state["relay"])
    r = t.c.call("relay.rotate")
    check(r["key"] != old["key"] and r["key_id"] != e2e.key_id(e2e.parse_key(old["key"])), "key not replaced")
    check(identity(t)["key"] == r["key"] and identity(t)["secret"] == old["secret"], "identity file")
    check(wait_until(lambda: ch.closed, 10), "rotate left the viewer connected")
    check(t.c.wait_state("relay", lambda s: not s["viewers"] and s["key_id"] == r["key_id"], 5), t.c.state["relay"])
    try:
        connect(ctx, old["key"]).close()
        check(False, "the old key still works")
    except e2e.WrongKey:
        pass
    connect(ctx, r["key"]).close()


# ================================================================== the HTTP transport (RLY-27)
FALLBACK_LOG = "the device manager does not take WebSockets here - using HTTP"


@contextlib.contextmanager
def transport(value):
    """ARSTRO_RELAY_TRANSPORT for this process's own client (pipe.connect reads it every time)."""
    old = os.environ.get("ARSTRO_RELAY_TRANSPORT")
    os.environ["ARSTRO_RELAY_TRANSPORT"] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("ARSTRO_RELAY_TRANSPORT", None)
        else:
            os.environ["ARSTRO_RELAY_TRANSPORT"] = old


class NoUpgradeProxy:
    """A reverse proxy like an nginx without the Upgrade lines: every request goes to the relay as
    plain HTTP without Upgrade / Connection (so its WebSocket routes answer 426), answers are
    buffered whole, the client side is HTTP/1.1 keep-alive."""

    def __init__(self, upstream_port):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _forward(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("connection", "upgrade", "keep-alive")}
                up = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=60)
                try:
                    up.request(self.command, self.path, body, hdrs)
                    r = up.getresponse()
                    data = r.read()
                finally:
                    up.close()
                self.send_response(r.status, r.reason)
                for k, v in r.getheaders():
                    if k.lower() not in ("connection", "keep-alive", "transfer-encoding", "content-length", "date",
                                         "server"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _forward

            def log_message(self, *a):
                pass

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass                             # clients cut their requests on purpose (pipe tests)

        self.srv = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def ws_device(ctx):
    """`t` online at the relay (the earlier tests do that; -k runs need it here)."""
    if ctx.device_id is None:
        ctx.t.c.call("relay.set", url=ctx.relay_url, token=ctx.join, name="Relay test", enabled=True)
        check(ctx.t.c.wait_state("relay", lambda s: s["state"] == "online", 20), ctx.t.c.state.get("relay"))
        ctx.device_id = ctx.t.c.state["relay"]["id"]
    return ctx.t


def http_device(ctx):
    """A second instance forced onto pipes (ARSTRO_RELAY_TRANSPORT=http), online at the relay."""
    if getattr(ctx, "th", None) is None:
        th = Instance(["system", "relay"], app="test-relay-http")
        th.env["ARSTRO_RELAY_TRANSPORT"] = "http"
        ctx.th = th
        th.start()
        th.c.call("relay.set", url=ctx.relay_url, token=ctx.join, name="Relay HTTP test", enabled=True)
    check(ctx.th.c.wait_state("relay", lambda s: s["state"] == "online", 20), ctx.th.c.state.get("relay"))
    return ctx.th


def echo_tcp(ch, port, size, breaker=None):
    """Send `size` random bytes through a tcp stream to the echo server and read them back
    (a reader thread: more than WINDOW in flight needs both directions moving)."""
    s = ch.tcp(port)
    blob = os.urandom(size)
    got = bytearray()
    err = []

    def reader():
        try:
            while len(got) < len(blob):
                ev = s.next(20)
                if ev[0] != "data":
                    err.append(ev)
                    return
                got.extend(ev[2])
        except Exception as e:               # noqa: BLE001 - reported below
            err.append(e)
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    for i in range(0, len(blob), 32 * 1024):
        s.send_data(blob[i:i + 32 * 1024])
        if breaker:
            breaker(i)
    th.join(60)
    check(not err and bytes(got) == blob, "echo differs: %d of %d bytes, %s" % (len(got), len(blob), err[:1]))
    s.send_json({"t": "end"})
    ev = s.next(10)
    check(ev[0] == "json" and ev[1]["t"] == "end", ev)
    s.close()


@test("RLY-27", "RLY-04", "RLY-06")
def device_and_viewer_over_http_only(ctx):
    th = http_device(ctx)
    ws_device(ctx)
    st = th.c.call("relay.status")
    check(st["transport"] == "http" and st["error"] is None, st)
    if ctx.t.env.get("ARSTRO_RELAY_TRANSPORT", "auto") != "http":
        check(ctx.t.c.call("relay.status")["transport"] == "ws", "the WebSocket device must say ws")
    d = {x["id"]: x for x in devices(ctx)}.get(st["id"])
    check(d and d["online"] and d["name"] == "Relay HTTP test", d)
    r = subprocess.run([sys.executable, "-m", "arstro_remote", "--json", "relay", "status"], cwd=os.path.join(ROOT, "server"),
                       env=th.env, capture_output=True, text=True, timeout=60)
    check(r.returncode == 0 and json.loads(r.stdout)["transport"] == "http", r.stdout[-300:] + r.stderr[-300:])
    r = subprocess.run([sys.executable, "-m", "arstro_remote", "relay", "status"], cwd=os.path.join(ROOT, "server"),
                       env=th.env, capture_output=True, text=True, timeout=60)
    check("online (HTTP)" in r.stdout, r.stdout)
    key = identity(th)["key"]
    with transport("http"):
        try:
            client.Relay(ctx.relay_url, timeout=15).connect(st["id"], e2e.new_key()).close()
            check(False, "a wrong key was accepted")
        except e2e.WrongKey:
            pass
        ch = client.Relay(ctx.relay_url, timeout=15).connect(st["id"], key)
    try:
        check(ch.transport == "http" and ch.info["t"] == "ready" and ch.info["id"] == st["id"], ch.info)
        check(th.c.wait_state("relay", lambda s: len(s["viewers"]) == 1, 5), th.c.state["relay"])
        status, _r, _h, body = ch.http("GET", th.port, "/api/ping")
        check(status == 200 and "version" in json.loads(body), body[:200])
        # 3 MB up through an http stream (many up batches), 3 MB down
        port = ctx.headers.server_address[1]
        blob = os.urandom(3 << 20)
        status, _r, _h, body = ch.http("PUT", port, "/upload", {"Content-Type": "application/octet-stream"}, blob)
        got = json.loads(body)
        check(status == 201 and got["body"] == len(blob) and got["sha"] == hashlib.sha256(blob).hexdigest(), got)
        status, _r, _h, body = ch.http("GET", port, "/big/%d" % (3 << 20))
        check(status == 200 and body == bytes(range(256)) * ((3 << 20) // 256), "3 MB download differs")
        # a WebSocket stream: the instance's own /ws
        s = ch.stream({"t": "ws", "port": th.port, "path": "/ws", "protocols": [],
                       "headers": [["Authorization", "Bearer " + th.token]]})
        ev = s.next(10)
        check(ev[0] == "json" and ev[1]["t"] == "open", ev)
        s.send_data(json.dumps({"op": "hello", "id": 7, "app": "relay-http-test"}).encode(), e2e.F_FIN | e2e.F_TEXT)
        reply, end = None, time.monotonic() + 10
        while reply is None and time.monotonic() < end:
            ev = s.next(10)
            check(ev[0] == "data", ev)
            reply = next((m for m in frames(ev[2]) if m.get("id") == 7), None)
        check(reply and reply["ok"] and reply["data"]["slot"] == "t", reply)
        s.send_json({"t": "close", "code": 1000, "reason": "done"})
        s.close()
        echo_tcp(ch, ctx.echo.port, 1 << 20)
        pipe_id = ch.conn._path.rsplit("/", 1)[1]
    finally:
        ch.close()
    check(th.c.wait_state("relay", lambda s: not s["viewers"], 10), "the device kept the viewer after close")
    # the standalone client: the files the device manager serves at B/client/, alone in a folder
    d = tempfile.mkdtemp(prefix="arstro-client-")
    try:
        src = os.path.join(ROOT, "server", "arstro_remote")
        for name, path in (("arstro_relay_client.py", "relay/client.py"), ("e2e.py", "relay/e2e.py"),
                           ("ws.py", "web/ws.py"), ("pipe.py", "relay/pipe.py")):
            shutil.copy(os.path.join(src, path), os.path.join(d, name))
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ARSTRO_RELAY_URL", "ARSTRO_RELAY_KEY")}
        env["ARSTRO_RELAY_TRANSPORT"] = "http"
        r = subprocess.run([sys.executable, "arstro_relay_client.py", "check", st["id"], "--key", key,
                            "--relay", ctx.relay_url], cwd=d, env=env, capture_output=True, text=True, timeout=60)
        check(r.returncode == 0 and "key ok" in r.stdout and "over HTTP" in r.stdout, r.stdout + r.stderr[-500:])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    logs = open(th.log, encoding="utf-8", errors="replace").read()
    relay_log = open(os.path.join(ctx.relay_dir, "relay.log"), encoding="utf-8", errors="replace").read()
    check(pipe_id not in logs and pipe_id not in relay_log, "a pipe id is in a log")
    check("tunnel up" in logs and "over HTTP" in logs and FALLBACK_LOG not in logs, "forced HTTP: no fallback line")
    bad = [l for l in logs.splitlines() if "Traceback" in l or " ERROR " in l]
    check(not bad, "errors in the server log:\n" + "\n".join(bad[:10]))


@test("RLY-27")
def pipe_keeps_order_when_polls_break(ctx):
    """Client pipes to the WebSocket device: the down and up requests are cut again and again
    while 2 MB go round through the echo - nothing lost, nothing doubled, nothing reordered."""
    ws_device(ctx)
    with transport("http"):
        ch = connect(ctx)
    try:
        check(ch.transport == "http", ch.transport)
        broken = [0]

        def breaker(i):
            if i % (128 * 1024) == 0:
                for which in ("down", "up"):
                    if getattr(ch.conn._conns.get(which), "sock", None) is not None:
                        broken[0] += 1
                    ch.conn._abort(which)
        echo_tcp(ch, ctx.echo.port, 2 << 20, breaker)
        check(broken[0] >= 10 and ch.conn.retries >= 5, "only %d connections were cut, %d requests sent again" % (
            broken[0], ch.conn.retries))
        status, _r, _h, body = ch.http("GET", ctx.t.port, "/api/ping")
        check(status == 200, status)
        check(not ch.closed, ch.reason)
    finally:
        ch.close()
    # a raw pipe: ack / seq rules seen from outside
    conn = pipe.open_pipe(ctx.relay_url, "/api/connect/" + ctx.device_id, timeout=10)
    try:
        conn.settimeout(15)
        op, data = conn.recv_message()
        check(op == ws.OP_TEXT and json.loads(data) == {"t": "relay", "ok": True}, data)
    finally:
        conn.close()
    check(conn.closed, "closed")
    try:
        conn.send_text("{}")
        check(False, "a closed pipe took a message")
    except ws.WSClosed:
        pass


def forced(ctx, what):
    """The fallback tests need `t` in auto mode: None, or skip when the suite forces a transport."""
    mode = ctx.t.env.get("ARSTRO_RELAY_TRANSPORT", "auto")
    if mode != "auto":
        print("SKIP  %s: the whole suite runs with ARSTRO_RELAY_TRANSPORT=%s" % (what, mode))
    return mode != "auto"


def fallback_through(ctx, proxy_url, what):
    """`t` (auto) and a viewer (auto) behind `proxy_url`: WebSockets get 426, both go over HTTP."""
    t = ctx.t
    url = proxy_url + BASE
    t.c.call("relay.set", url=url)
    try:
        check(t.c.wait_state("relay", lambda s: s["state"] == "online" and s["url"] == url and s["transport"] == "http",
                             30), "%s: the device did not fall back: %s" % (what, t.c.state["relay"]))
        log = open(t.log, encoding="utf-8", errors="replace").read()
        check(FALLBACK_LOG in log and "426" in log, "%s: the fallback is not logged" % what)
        with transport("auto"):
            ch = client.Relay(url, timeout=15).connect(ctx.device_id, identity(t)["key"])
        try:
            check(ch.transport == "http", "%s: the client did not fall back" % what)
            status, _r, _h, body = ch.http("GET", t.port, "/api/ping")
            check(status == 200 and "version" in json.loads(body), body[:200])
            blob = os.urandom(3 << 19)                    # 1.5 MB: more than nginx's client_max_body_size
            status, _r, _h, body = ch.http("PUT", ctx.headers.server_address[1], "/up", {}, blob)
            check(status == 201 and json.loads(body)["sha"] == hashlib.sha256(blob).hexdigest(), body[:200])
            echo_tcp(ch, ctx.echo.port, 1 << 20)
        finally:
            ch.close()
    finally:
        t.c.call("relay.set", url=ctx.relay_url)
    check(t.c.wait_state("relay", lambda s: s["state"] == "online" and s["url"] == ctx.relay_url and s["transport"] == "ws",
                         20), "no WebSockets again at the relay itself: %s" % t.c.state["relay"])


def relay_port(ctx):
    return int(urllib.parse.urlsplit(ctx.relay_url).port)


@test("RLY-27")
def falls_back_to_http_behind_a_proxy_without_websockets(ctx):
    if forced(ctx, "python proxy"):
        return
    ws_device(ctx)
    px = NoUpgradeProxy(relay_port(ctx))
    try:
        fallback_through(ctx, px.url, "python proxy")
    finally:
        px.close()


@test("RLY-27")
def falls_back_behind_nginx_1_14(ctx):
    if forced(ctx, "nginx 1.14"):
        return
    ws_device(ctx)
    sys.path.insert(0, os.path.join(ROOT, "relay", "tests"))
    from nginx_proxy import NginxProxy, NginxUnavailable
    proxy = NginxProxy(relay_port(ctx))
    try:
        url = proxy.start()
    except NginxUnavailable as e:
        print("SKIP  falls_back_behind_nginx_1_14: %s (needs Docker and the nginx:1.14 image)" % e)
        return
    try:
        fallback_through(ctx, url, "nginx 1.14")
        log = proxy.logs()
        check(re.search(r'"GET /\S+/api/agent HTTP/1.1" 426 ', log) and '/api/pipe/open HTTP/1.1" 200 ' in log,
              "nginx did not see the 426 and the pipes:\n" + log[-1500:])
    finally:
        proxy.stop()


@test("RLY-05", "RLY-07")
def wrong_keys_are_logged_then_locked_out(ctx):
    t = ctx.t
    before = t.c.call("relay.status")["wrong_keys"]
    wrong = 0
    for _ in range(7):                  # the 6th wrong key within a minute locks (earlier tests made some)
        try:
            connect(ctx, e2e.new_key()).close()
            check(False, "wrong key accepted")
        except e2e.WrongKey:
            wrong += 1
            check(wait_until(lambda: t.c.call("relay.status")["wrong_keys"] == before + wrong, 5), "not counted")
        except client.RelayError as e:
            check("too many wrong keys - wait a minute" in str(e), e)
            break
    check(1 <= wrong <= 6 and before + wrong >= 6, "locked after %d (+%d earlier) wrong keys" % (wrong, before))
    for key in (e2e.new_key(), identity(t)["key"]):
        try:
            connect(ctx, key).close()
            check(False, "not locked out")
        except client.RelayError as e:
            check("too many wrong keys - wait a minute" in str(e), e)
    log = open(t.log, encoding="utf-8", errors="replace").read()
    check(re.search(r"WARNING .*wrong access key", log) and "refusing new viewers for 60 s" in log, "not logged")


@test("RLY-01")
def device_reconnects_when_the_device_manager_comes_back(ctx):
    t = ctx.t
    ctx.relay.terminate()
    ctx.relay.wait(10)
    check(t.c.wait_state("relay", lambda s: s["state"] in ("error", "connecting") and s["error"], 15),
          t.c.state["relay"])
    time.sleep(1.5)                                    # a failed attempt or two: back-off grows
    start_relay(ctx)
    check(t.c.wait_state("relay", lambda s: s["state"] == "online", 20), t.c.state["relay"])
    d = {x["id"]: x for x in devices(ctx)}.get(ctx.device_id)
    check(d and d["online"], d)
    log = open(t.log, encoding="utf-8", errors="replace").read()
    check(log.count("tunnel up") >= 2 and "tunnel down" in log, "reconnect not logged")


@test("RLY-02", "RLY-07")
def key_token_and_secret_never_leave_in_topic_or_log(ctx):
    t = ctx.t
    ident = identity(t)
    secrets_ = [ident["key"], ident["secret"], ctx.join, e2e.b64e(e2e.parse_key(ident["key"]))]
    st = json.dumps(t.c.call("relay.status"))
    snap = json.dumps(t.c.call("state.get"))
    for s in secrets_:
        check(s not in st and s not in snap, "a secret is in the relay topic")
    log = open(t.log, encoding="utf-8", errors="replace").read()
    for s in secrets_:
        check(s not in log, "a secret is in the log")
    check("access key shown to session" in log and "settings changed by session" in log and "join token" in log,
          "key view / setting changes not logged")
    bad = [l for l in log.splitlines() if "Traceback" in l or " ERROR " in l or "crashed" in l]
    check(not bad, "errors in the server log:\n" + "\n".join(bad[:10]))


# ================================================================== setup
class Ctx:
    pass


def start_relay(ctx):
    """The device manager (relay/arstro_relay) on ctx.relay_url, its data in ctx.relay_dir."""
    ctx.relay = subprocess.Popen([sys.executable, "-m", "arstro_relay"], cwd=os.path.join(ROOT, "relay"),
                                 env=ctx.relay_env, stdout=ctx.relay_log, stderr=subprocess.STDOUT)

    def health():
        try:
            with urllib.request.urlopen(ctx.relay_url + "/api/health", timeout=1) as r:
                return json.loads(r.read()).get("ok")
        except OSError:
            return False
    if not wait_until(health, 15):
        print("WARNING: the device manager did not start (see %s/relay.log) - integration tests fail" % ctx.relay_dir)


def setup():
    ctx = Ctx()
    ctx.echo = EchoTCP()
    ctx.headers = header_server()
    ctx.join = "join-" + secrets.token_urlsafe(12)
    ctx.relay_dir = tempfile.mkdtemp(prefix="arstro-relay-")
    port = free_port()
    ctx.relay_url = "http://127.0.0.1:%d%s" % (port, BASE)
    ctx.relay_env = dict(os.environ, RELAY_HOST="127.0.0.1", RELAY_PORT=str(port), RELAY_DATA=ctx.relay_dir,
                         RELAY_JOIN_TOKEN=ctx.join, RELAY_RATE_LIMIT="1000/10", RELAY_HEARTBEAT="2")
    ctx.relay_env.pop("RELAY_PASSWORD", None)
    ctx.relay_log = open(os.path.join(ctx.relay_dir, "relay.log"), "a")
    start_relay(ctx)
    ctx.t = Instance(["system", "relay"], app="test-relay")
    ctx.t.start()
    ctx.device_id = None
    return ctx


def teardown(ctx):
    ctx.t.stop()
    if getattr(ctx, "th", None) is not None:
        ctx.th.stop()
    if ctx.relay.poll() is None:
        ctx.relay.terminate()
        try:
            ctx.relay.wait(10)
        except subprocess.TimeoutExpired:
            ctx.relay.kill()
    ctx.relay_log.close()
    ctx.headers.shutdown()
    shutil.rmtree(ctx.relay_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run(setup, teardown, __doc__))
