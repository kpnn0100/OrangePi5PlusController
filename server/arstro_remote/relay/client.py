"""Arstro device manager client: reach any port of a device, end-to-end encrypted (RLY-26).

For agents, scripts and the CLI (`arstro-remote relay devices|ports|http|forward`). Standalone
too: download `arstro_relay_client.py`, `e2e.py` and `ws.py` from `<relay>/client/` into one
folder (needs Python 3.8+ and `cryptography`) and run

    python3 arstro_relay_client.py devices --relay https://abc.example.com/arstro/internal/devices
    python3 arstro_relay_client.py ports   <device> --key ak1_...
    python3 arstro_relay_client.py http    <device> 8080 /api/ping
    python3 arstro_relay_client.py http    <device> 8080 /api/op/stats.get -X POST -d '{}' -H 'Authorization: Bearer <pw>'
    python3 arstro_relay_client.py forward <device> 8080 --listen 127.0.0.1:18080

`forward` turns the device's port into a local one: anything that speaks TCP (a browser,
curl, `arstro-remote --url http://127.0.0.1:18080`, ssh -p ...) then works as if it were on
the device's LAN, through one encrypted channel. Settings come from the options or the
environment: ARSTRO_RELAY_URL, ARSTRO_RELAY_KEY (or --key-file), ARSTRO_RELAY_PASSWORD (only
when the device manager has a viewer password). docs/relay/AGENT_API.md has the details.
"""

import argparse
import itertools
import json
import os
import queue
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

try:
    from . import e2e
    from ..web import ws as wsmod
except ImportError:                      # standalone: e2e.py and ws.py next to this file
    import e2e
    import ws as wsmod

OP_TEXT, OP_BINARY = 1, 2

# The command line (docs/relay/AGENT_API.md documents each one; test_relay_docs.py checks it)
COMMANDS = {
    "devices": "list the devices",
    "check": "check the access key (handshake only)",
    "ports": "list the device's listening TCP ports",
    "http": "one HTTP request to a port of the device",
    "forward": "serve a local port that leads to a port of the device",
}


class RelayError(Exception):
    """The device manager or the device refused (the message says why)."""


def ws_url(url):
    u = urllib.parse.urlsplit(url)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(u.scheme)
    if not scheme or not u.hostname:
        raise ValueError("the device manager URL must be http(s)://host/path (got %r)" % url)
    return urllib.parse.urlunsplit((scheme, u.netloc, u.path.rstrip("/"), "", ""))


class Relay:
    """A device manager: https://<server>/arstro/internal/devices."""

    def __init__(self, url, password=None, timeout=15):
        if not url:
            raise ValueError("no device manager URL (--relay or ARSTRO_RELAY_URL)")
        self.url = url.rstrip("/")
        ws_url(self.url)                 # validate early
        self.password = password
        self.timeout = timeout

    def headers(self):
        return {"Authorization": "Bearer " + self.password} if self.password else {}

    def devices(self):
        req = urllib.request.Request(self.url + "/api/devices", headers=self.headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())["devices"]
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RelayError("the device manager needs its viewer password (--password)")
            raise RelayError("the device manager answered %d %s" % (e.code, e.reason))

    def connect(self, device_id, key):
        return Channel(self, device_id, key)


class Stream:
    """One stream of a channel: the reader thread queues events, the user takes them."""

    def __init__(self, channel, sid):
        self.channel = channel
        self.sid = sid
        self.events = queue.Queue()
        self._inflight = 0
        self._unacked = 0
        self._cond = threading.Condition()
        self.closed = False

    # reader side
    def _acked(self, n):
        with self._cond:
            self._inflight = max(0, self._inflight - n)
            self._cond.notify_all()

    def _push(self, ev):
        self.events.put(ev)

    def _end(self, reason):
        with self._cond:
            self.closed = True
            self._cond.notify_all()
        self.events.put(("closed", reason))

    # user side
    def send_json(self, obj):
        self.channel.send(e2e.K_JSON, self.sid, json.dumps(obj, separators=(",", ":")).encode())

    def send_data(self, data, flags=0):
        data = memoryview(bytes(data))
        for i in range(0, max(len(data), 1), e2e.CHUNK):
            piece = data[i:i + e2e.CHUNK]
            with self._cond:
                while self._inflight >= e2e.WINDOW and not self.closed:
                    self._cond.wait(1)
                if self.closed:
                    raise RelayError("the stream was closed")
                self._inflight += len(piece)
            last = i + e2e.CHUNK >= len(data)
            self.channel.send(e2e.K_DATA, self.sid, piece, flags if last else flags & ~e2e.F_FIN)

    def next(self, timeout=None):
        """-> ("json", msg) | ("data", flags, bytes) | ("closed", reason)."""
        try:
            ev = self.events.get(timeout=timeout)
        except queue.Empty:
            raise RelayError("the device did not answer in time")
        if ev[0] == "data":
            self._unacked += len(ev[2])
            if self._unacked >= e2e.WINDOW // 4:
                self.send_json({"t": "ack", "n": self._unacked})
                self._unacked = 0
        return ev

    def close(self):
        self.channel._streams.pop(self.sid, None)


class Channel:
    """An end-to-end encrypted channel to one device (PROTOCOL.md section 4)."""

    def __init__(self, relay, device_id, key):
        raw = e2e.parse_key(key)
        self.relay = relay
        url = ws_url(relay.url) + "/api/connect/" + urllib.parse.quote(device_id, safe="")
        try:
            self.conn = wsmod.connect(url, relay.headers(), timeout=relay.timeout)
        except PermissionError:
            raise RelayError("the device manager needs its viewer password (--password)")
        self.conn.sock.settimeout(relay.timeout)
        first = self._text()
        if first.get("t") != "relay" or not first.get("ok"):
            self.conn.close()
            raise RelayError(first.get("error") or "the device manager refused the connection")
        cn, hello = e2e.viewer_hello()
        self.conn.send_text(json.dumps(hello))
        answer = self._text()
        try:
            self.secure, proof = e2e.viewer_check(raw, cn, answer)
        except e2e.WrongKey:
            self.conn.close()
            raise
        except e2e.ProtocolError as e:
            self.conn.close()
            raise RelayError(str(e))
        self.conn.send_text(json.dumps(proof))
        op, data = self.conn.recv_message()
        if op == OP_TEXT:
            self.conn.close()
            err = json.loads(data.decode()).get("error", "refused")
            raise (e2e.WrongKey if "wrong key" in err else RelayError)(err)
        kind, sid, _flags, payload = self.secure.open(data)
        self.info = e2e.decode_json(payload)
        if kind != e2e.K_JSON or sid != 0 or self.info.get("t") != "ready":
            self.conn.close()
            raise RelayError("the device did not send ready")
        self.conn.sock.settimeout(None)
        self._send_lock = threading.Lock()
        self._streams = {}
        self._ids = itertools.count(1)
        self.closed = False
        self.reason = None
        threading.Thread(target=self._reader, name="relay-channel", daemon=True).start()

    def _text(self):
        op, data = self.conn.recv_message()
        if op != OP_TEXT:
            raise RelayError("unexpected binary message during the handshake")
        return json.loads(data.decode())

    def send(self, kind, sid, payload, flags=0):
        with self._send_lock:            # nonce order must be wire order
            if self.closed:
                raise RelayError("the channel is closed: %s" % self.reason)
            self.conn.send_binary(self.secure.seal(kind, sid, payload, flags))

    def _reader(self):
        reason = "closed"
        try:
            while True:
                op, data = self.conn.recv_message()
                if op != OP_BINARY:
                    raise e2e.ProtocolError("text message after the handshake")
                kind, sid, flags, payload = self.secure.open(data)
                if sid == 0:
                    msg = e2e.decode_json(payload) if kind == e2e.K_JSON else {}
                    if msg.get("t") == "ping":
                        self.send(e2e.K_JSON, 0, json.dumps({"t": "pong"}).encode())
                    continue
                stream = self._streams.get(sid)
                if stream is None:
                    continue
                if kind == e2e.K_JSON:
                    msg = e2e.decode_json(payload)
                    if msg["t"] == "ack":
                        stream._acked(int(msg.get("n", 0)))
                    else:
                        stream._push(("json", msg))
                else:
                    stream._push(("data", flags, payload))
        except (wsmod.WSClosed, OSError) as e:
            reason = "connection lost (%s)" % e
        except e2e.ProtocolError as e:
            reason = str(e)
        self.closed = True
        self.reason = reason
        self.conn.close()
        for s in list(self._streams.values()):
            s._end(reason)

    def stream(self, opener):
        sid = next(self._ids)
        s = Stream(self, sid)
        self._streams[sid] = s
        s.send_json(opener)
        return s

    def close(self):
        self.closed = True
        self.reason = "closed by this side"
        self.conn.close()

    # --------------------------------------------------------------- requests
    def ports(self):
        s = self.stream({"t": "ports"})
        try:
            ev = s.next(timeout=self.relay.timeout)
            if ev[0] == "json" and ev[1]["t"] == "ports":
                return ev[1]["ports"]
            raise RelayError(_why(ev))
        finally:
            s.close()

    def http(self, method, port, path, headers=None, body=None, timeout=60):
        """-> (status, reason, [[k, v]], body bytes)."""
        headers = dict(headers or {})
        if body is not None and not any(k.lower() == "content-length" for k in headers):
            headers["Content-Length"] = str(len(body))     # some servers refuse chunked uploads (411)
        s = self.stream({"t": "http", "method": method, "port": int(port), "path": path,
                         "headers": [[k, v] for k, v in headers.items()], "body": body is not None})
        try:
            if body is not None:
                s.send_data(body)
                s.send_json({"t": "end"})
            ev = s.next(timeout=timeout)
            if ev[0] != "json" or ev[1]["t"] != "res":
                raise RelayError(_why(ev))
            head, out = ev[1], bytearray()
            while True:
                ev = s.next(timeout=timeout)
                if ev[0] == "data":
                    out += ev[2]
                elif ev[0] == "json" and ev[1]["t"] == "end":
                    return head["status"], head.get("reason", ""), head.get("headers", []), bytes(out)
                else:
                    raise RelayError(_why(ev))
        finally:
            s.close()

    def tcp(self, port, timeout=15):
        s = self.stream({"t": "tcp", "port": int(port)})
        ev = s.next(timeout=timeout)
        if ev[0] != "json" or ev[1]["t"] != "open":
            s.close()
            raise RelayError(_why(ev))
        return s


def _why(ev):
    if ev[0] == "json" and ev[1].get("t") == "error":
        return ev[1].get("error", "refused")
    if ev[0] == "closed":
        return "the channel closed: %s" % ev[1]
    return "unexpected answer from the device: %r" % (ev[1] if len(ev) > 1 else ev,)


# ------------------------------------------------------------------ port forward
def forward(relay, device_id, key, port, listen=("127.0.0.1", 0), ready=None, log=print):
    """Serve a local TCP port that leads to `port` on the device. Runs until interrupted.
    `ready(address)` is called once listening (tests use it)."""
    state = {"ch": None}
    lock = threading.Lock()

    def channel():
        with lock:
            if state["ch"] is None or state["ch"].closed:
                state["ch"] = relay.connect(device_id, key)
            return state["ch"]

    def pump(local):
        try:
            s = channel().tcp(port)
        except (RelayError, e2e.WrongKey, OSError) as e:
            log("forward: %s" % e)
            local.close()
            return
        done = threading.Event()

        def up():                          # local -> device
            try:
                while True:
                    data = local.recv(e2e.CHUNK)
                    if not data:
                        s.send_json({"t": "end"})
                        return
                    s.send_data(data)
            except (OSError, RelayError):
                try:
                    s.send_json({"t": "close"})
                except RelayError:
                    pass
                done.set()

        threading.Thread(target=up, daemon=True).start()
        try:
            while not done.is_set():
                ev = s.next()
                if ev[0] == "data":
                    local.sendall(ev[2])
                elif ev[0] == "json" and ev[1]["t"] == "end":
                    local.shutdown(socket.SHUT_WR)
                else:
                    break
        except OSError:
            pass
        finally:
            s.close()
            local.close()

    srv = socket.socket(socket.AF_INET6 if ":" in listen[0] else socket.AF_INET)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(listen)
    srv.listen(64)
    channel()                              # fail now on a wrong key / offline device
    addr = srv.getsockname()
    log("forwarding %s:%d -> %s port %d (Ctrl+C to stop)" % (addr[0], addr[1], device_id, port))
    if ready:
        ready(addr)
    try:
        while True:
            local, _ = srv.accept()
            threading.Thread(target=pump, args=(local,), daemon=True).start()
    finally:
        srv.close()


# ------------------------------------------------------------------ command line
def _key(args):
    if args.key:
        return args.key
    if args.key_file:
        with open(os.path.expanduser(args.key_file)) as f:
            return next((l.strip() for l in f if l.strip()), "")
    if os.environ.get("ARSTRO_RELAY_KEY"):
        return os.environ["ARSTRO_RELAY_KEY"]
    raise SystemExit("no access key: --key, --key-file or ARSTRO_RELAY_KEY "
                     "(the device shows it: arstro-remote relay key)")


def _listen(text):
    host, _, port = text.rpartition(":")
    return (host.strip("[]") or "127.0.0.1", int(port))


def main(argv=None):
    p = argparse.ArgumentParser(prog="arstro-relay-client",
                                description="Reach the ports of a device through an Arstro device manager.")
    p.add_argument("--relay", default=os.environ.get("ARSTRO_RELAY_URL"),
                   help="device manager URL, e.g. https://abc.example.com/arstro/internal/devices")
    p.add_argument("--password", default=os.environ.get("ARSTRO_RELAY_PASSWORD"),
                   help="the device manager's viewer password, if it has one")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)      # also accepted after the command
    common.add_argument("--relay", default=argparse.SUPPRESS)
    common.add_argument("--password", default=argparse.SUPPRESS)
    d = sub.add_parser("devices", help=COMMANDS["devices"], parents=[common])
    d.add_argument("--json", action="store_true")
    for name in ("check", "ports", "http", "forward"):
        s = sub.add_parser(name, help=COMMANDS[name], parents=[common])
        s.add_argument("device", help="device id (see devices)")
        s.add_argument("--key", help="the device's access key (ak1_...)")
        s.add_argument("--key-file", help="file holding the access key")
        s.add_argument("--json", action="store_true")
        if name in ("http", "forward"):
            s.add_argument("port", type=int)
        if name == "http":
            s.add_argument("path", nargs="?", default="/")
            s.add_argument("-X", "--method", default=None)
            s.add_argument("-H", "--header", action="append", default=[], help="'Name: value'")
            s.add_argument("-d", "--data", help="request body (@file reads a file)")
            s.add_argument("-i", "--include", action="store_true", help="print the status and headers")
        if name == "forward":
            s.add_argument("--listen", type=_listen, default=("127.0.0.1", 0),
                           help="local address:port (default 127.0.0.1 and a free port)")
    a = p.parse_args(argv)
    try:
        relay = Relay(a.relay, a.password)
        if a.cmd == "devices":
            devs = relay.devices()
            if a.json:
                print(json.dumps(devs, indent=2))
            for dv in ([] if a.json else devs):
                print("%-32s %-8s %-24s web %s" % (dv["id"], "online" if dv.get("online") else "offline",
                                                    dv.get("name", ""), dv.get("web") or "-"))
            return 0
        if a.cmd == "forward":
            forward(relay, a.device, _key(a), a.port, a.listen,
                    log=lambda m: print(m, file=sys.stderr, flush=True))
            return 0
        ch = relay.connect(a.device, _key(a))
        if a.cmd == "check":
            print(json.dumps(ch.info) if a.json else "key ok: %s (%s)" % (ch.info.get("name"), ch.info.get("hostname")))
        elif a.cmd == "ports":
            ports = ch.ports()
            if a.json:
                print(json.dumps(ports, indent=2))
            for pt in ([] if a.json else ports):
                print("%-6s %-16s %s" % (pt["port"], pt.get("addr", ""), pt.get("proc") or ""))
        elif a.cmd == "http":
            body = None
            if a.data is not None:
                body = open(a.data[1:], "rb").read() if a.data.startswith("@") else a.data.encode()
            headers = dict(h.split(":", 1) for h in a.header)
            headers = {k.strip(): v.strip() for k, v in headers.items()}
            if body is not None and not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json"
            status, reason, rh, out = ch.http(a.method or ("POST" if body is not None else "GET"),
                                              a.port, a.path, headers, body)
            if a.include:
                print("HTTP %d %s" % (status, reason))
                for k, v in rh:
                    print("%s: %s" % (k, v))
                print()
                sys.stdout.flush()
            sys.stdout.buffer.write(out)
            sys.stdout.flush()
            ch.close()
            return 0 if status < 400 else 1
        ch.close()
        return 0
    except e2e.WrongKey as e:
        print("error: %s" % e, file=sys.stderr)
        return 3
    except (RelayError, ValueError, OSError, wsmod.WSClosed) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
