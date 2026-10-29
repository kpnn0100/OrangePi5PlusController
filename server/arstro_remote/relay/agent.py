"""The device side of the device manager tunnel (RLY-01, RLY-03, RLY-04, RLY-05).

    device manager  <──WS <url>/api/agent (control: hello, ping, open, refuse)── Agent
                    <──WS <url>/api/agent/data (one per viewer)──────────────── Channel
                                                                                  │ Stream per request
                                                                                  ▼
                                                            127.0.0.1:<port> (then [::1]:<port>)

`Agent` keeps the control connection (reconnects with back-off 1 -> 60 s, +-20 % jitter) and
dials a data connection for every `open`. `Channel` is one viewer: the end-to-end handshake as
the DEVICE (e2e.py), then encrypted frames carrying streams (PROTOCOL.md 4.3 / 4.4): http,
ws, tcp, ports.

Threads, not asyncio, like the rest of the daemon. Per channel: a reader (never blocks: it only
queues), a writer (the only thread that seals and sends, under the channel's send lock - so the
nonce counter order is the wire order), and per stream one thread that talks to the local port
(two for ws / tcp: one each way). Flow control (WINDOW per stream and direction) blocks only a
stream's own sender.

Never logs the access key, the join token or the device secret.
"""

import base64
import collections
import hashlib
import http.client
import itertools
import json
import logging
import os
import random
import re
import socket
import struct
import threading
import time
import urllib.parse

from . import e2e
from ..web import ws as wsmod

log = logging.getLogger("arstro.relay")

HEARTBEAT = 20                # s, unless the relay's welcome says otherwise
BACKOFF_MIN, BACKOFF_MAX = 1.0, 60.0
CONTROL_TIMEOUT = 10          # s: TCP/TLS/WebSocket connect, welcome, linked
MAX_CHANNELS = 16             # viewer channels at once (the relay allows 32 per device)
MAX_STREAMS = 128             # open streams per channel
HANDSHAKE_SEC = 15
CONNECT_SEC = 5               # local connect
WRONG_MAX = 5                 # more wrong proofs than this within WRONG_WINDOW ...
WRONG_WINDOW = 60
LOCKOUT_SEC = 60              # ... refuse new channels this long
QUEUE_MAX = 2 * e2e.WINDOW    # DATA bytes waiting for one channel's writer before senders wait
BODY_BUFFER = 1 << 20         # a request body without Content-Length up to this size gets one
TOO_MANY_WRONG = "too many wrong keys - wait a minute"

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
       "te", "trailer", "transfer-encoding", "upgrade"}
_PATH_RE = re.compile(r"^/[^\s\x00-\x1f\x7f]*$")
_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class TunnelError(Exception):
    """The control connection failed (the message is shown as the relay state's error)."""


class StreamError(Exception):
    """A stream cannot go on: the message goes to the viewer as {"t": "error"}."""


class Closed(Exception):
    """The channel is closed."""


def ws_base(url):
    """http(s)://host/base -> ws(s)://host/base (no trailing slash)."""
    u = urllib.parse.urlsplit(url)
    scheme = {"https": "wss", "http": "ws"}.get(u.scheme)
    if not scheme or not u.hostname:
        raise ValueError("the device manager URL must be http(s)://host/path")
    return urllib.parse.urlunsplit((scheme, u.netloc, u.path.rstrip("/"), "", ""))


def _describe(e):
    """A short sentence for a connection failure (shown in the state)."""
    if isinstance(e, PermissionError):
        return "the device manager refused the connection (401)"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, socket.timeout):
        return "timed out"
    if isinstance(e, socket.gaierror):
        return "unknown host (%s)" % (e.strerror or e)
    if isinstance(e, wsmod.WSClosed):
        return "connection closed (%s)" % e
    text = getattr(e, "strerror", None) or str(e) or type(e).__name__
    return text


def _recv_json(conn):
    op, data = conn.recv_message()
    if op != wsmod.OP_TEXT:
        raise TunnelError("unexpected binary message from the device manager")
    try:
        msg = json.loads(data.decode())
    except (ValueError, UnicodeDecodeError):
        raise TunnelError("bad message from the device manager")
    if not isinstance(msg, dict):
        raise TunnelError("bad message from the device manager")
    return msg


def _send_json(conn, obj):
    conn.send_text(json.dumps(obj, separators=(",", ":")))


# ------------------------------------------------------------------------- local side
def local_connect(port, timeout=CONNECT_SEC):
    """A TCP connection to 127.0.0.1:<port>, else [::1]:<port>. Never another host."""
    refused = timed_out = False
    last = None
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port, 0, 0))):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
        except OSError as e:
            last = e
            continue
        s.settimeout(timeout)
        try:
            s.connect(addr)
        except ConnectionRefusedError as e:
            refused, last = True, e
            s.close()
            continue
        except socket.timeout as e:
            timed_out, last = True, e
            s.close()
            continue
        except OSError as e:
            last = e
            s.close()
            continue
        s.settimeout(None)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return s
    if refused:
        raise StreamError("nothing listens on port %d" % port)
    if timed_out:
        raise StreamError("port %d did not answer within %d s" % (port, timeout))
    raise StreamError("cannot connect to port %d: %s" % (port, _describe(last)))


def _check_header(k, v):
    if not k or not _TOKEN_RE.match(k) or any(c in v for c in "\r\n\0"):
        raise StreamError("bad header %r" % k[:40])


def request_headers(headers, port, addr, websocket=False):
    """The viewer's headers with PROTOCOL.md 4.4 applied: hop-by-hop dropped, Host and Origin
    pointed at 127.0.0.1:<port>, Accept-Encoding identity, X-Forwarded-For the viewer."""
    if headers is None:
        headers = []
    if not isinstance(headers, list):
        raise StreamError("headers must be a list of [name, value]")
    items = []
    for item in headers:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise StreamError("headers must be a list of [name, value]")
        k, v = str(item[0]).strip(), str(item[1]).strip()
        _check_header(k, v)
        items.append((k, v))
    listed = set()                          # Connection: X-Foo makes X-Foo hop-by-hop too
    for k, v in items:
        if k.lower() == "connection":
            listed.update(t.strip().lower() for t in v.split(",") if t.strip())
    out = [("Host", "127.0.0.1:%d" % port)]
    for k, v in items:
        lk = k.lower()
        if lk in HOP or lk in listed or lk.startswith("proxy-") or lk in ("host", "accept-encoding", "x-forwarded-for"):
            continue
        if websocket and lk.startswith("sec-websocket-"):
            continue
        if lk == "origin":
            v = "http://127.0.0.1:%d" % port
        out.append((k, v))
    out.append(("Accept-Encoding", "identity"))
    if addr:
        out.append(("X-Forwarded-For", str(addr)))
    return out


def response_headers(pairs):
    listed = set()
    for k, v in pairs:
        if k.lower() == "connection":
            listed.update(t.strip().lower() for t in v.split(",") if t.strip())
    return [[k, v] for k, v in pairs
            if k.lower() not in HOP and k.lower() not in listed and not k.lower().startswith("proxy-")]


class LocalWS(wsmod.WSConnection):
    """A client WebSocket to a local port that remembers the close code and reason."""

    close_code = None
    close_reason = ""

    def _frame(self):
        fin, opcode, data = super()._frame()
        if opcode == wsmod.OP_CLOSE and self.close_code is None:
            if len(data) >= 2:
                self.close_code = struct.unpack(">H", data[:2])[0]
                self.close_reason = data[2:].decode("utf-8", "replace")
            else:
                self.close_code = 1005
        return fin, opcode, data

    def close_with(self, code, reason=""):
        if not self.closed:
            if not isinstance(code, int) or code in (1005, 1006, 1015) or not (1000 <= code <= 4999):
                code = 1000
            try:
                self._send(wsmod.OP_CLOSE, struct.pack(">H", code) + str(reason or "").encode()[:123])
            except (OSError, wsmod.WSClosed):
                pass
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def local_websocket(port, path, headers, protocols, addr):
    """WebSocket client handshake to the local port -> (LocalWS, negotiated protocol)."""
    if protocols is None:
        protocols = []
    if not isinstance(protocols, list) or not all(isinstance(p, str) and _TOKEN_RE.match(p) for p in protocols):
        raise StreamError("bad WebSocket protocols")
    hdrs = request_headers(headers, port, addr, websocket=True)
    sock = local_connect(port)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        lines = ["GET %s HTTP/1.1" % path] + ["%s: %s" % kv for kv in hdrs]
        lines += ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Key: " + key,
                  "Sec-WebSocket-Version: 13"]
        if protocols:
            lines.append("Sec-WebSocket-Protocol: " + ", ".join(protocols))
        sock.settimeout(CONTROL_TIMEOUT)
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise StreamError("port %d closed the connection during the WebSocket handshake" % port)
            resp += chunk
            if len(resp) > 65536:
                raise StreamError("port %d sent a bad WebSocket handshake" % port)
        head, rest = resp.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            raise StreamError("port %d answered %s instead of a WebSocket" % (port, " ".join(status[1:]) or "nothing"))
        fields = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
        expect = base64.b64encode(hashlib.sha1((key + wsmod.GUID).encode()).digest()).decode()
        if fields.get("sec-websocket-accept") != expect:
            raise StreamError("port %d sent a bad Sec-WebSocket-Accept" % port)
        sock.settimeout(None)
    except socket.timeout:
        sock.close()
        raise StreamError("port %d did not finish the WebSocket handshake" % port)
    except (StreamError, OSError):
        sock.close()
        raise
    buf = [rest]

    def read(n):
        if buf[0]:
            out, buf[0] = buf[0][:n], buf[0][n:]
            return out
        return sock.recv(n)

    return LocalWS(sock, read=read, client=True), fields.get("sec-websocket-protocol", "")


def listening_ports(allow=None):
    """[{port, addr, proc}] of the listening TCP ports, one per port (the wildcard address when
    it listens on several; the process name when this user may see it)."""
    import psutil
    names = {}
    found = {}
    for c in psutil.net_connections(kind="tcp"):
        if c.status != psutil.CONN_LISTEN or not c.laddr:
            continue
        port, ip = c.laddr.port, c.laddr.ip
        if allow is not None and port not in allow:
            continue
        if c.pid and c.pid not in names:
            try:
                names[c.pid] = psutil.Process(c.pid).name()
            except (psutil.Error, OSError):
                names[c.pid] = None
        entry = found.setdefault(port, {"port": port, "addr": ip, "proc": None})
        if ip in ("0.0.0.0", "::") and entry["addr"] not in ("0.0.0.0", "::"):
            entry["addr"] = ip
        entry["proc"] = entry["proc"] or names.get(c.pid)
    return [found[p] for p in sorted(found)]


# ------------------------------------------------------------------------- streams
END = object()


class Stream:
    """One stream of a channel. The channel's reader only queues into it (`push`, `acked`); the
    stream's own threads talk to the local port and wait for flow control."""

    def __init__(self, channel, sid, opener):
        self.ch = channel
        self.sid = sid
        self.opener = opener
        self.kind = opener["t"]
        self.port = opener.get("port")
        self.cv = threading.Condition()
        self.inq = collections.deque()       # ("data", flags, bytes) | END | ("close", code, reason)
        self.queued = 0                      # V->D bytes waiting in inq
        self.inflight = 0                    # D->V bytes not acknowledged yet
        self.unacked = 0                     # V->D bytes consumed, not acknowledged yet
        self.done = False
        self.local = None                    # socket / LocalWS, closed on abort
        self.sent_end = False
        self.got_end = False

    # ------------------------------------------------------------ reader side (never blocks)
    def push(self, item):
        with self.cv:
            if self.done:
                return
            if isinstance(item, tuple) and item[0] == "data":
                self.queued += len(item[2])
                if self.queued > e2e.WINDOW + e2e.CHUNK:
                    self.done = True
                    self.cv.notify_all()
                    overflow = True
                else:
                    overflow = False
                    self.inq.append(item)
            else:
                overflow = False
                self.inq.append(item)
            self.cv.notify_all()
        if overflow:
            self.fail("the viewer sent more than the flow-control window")

    def acked(self, n):
        with self.cv:
            self.inflight = max(0, self.inflight - max(0, int(n)))
            self.cv.notify_all()

    def abort(self):
        """Cancel / abort: drop the local connection now (unblocks the stream's threads)."""
        with self.cv:
            self.done = True
            self.cv.notify_all()
        local = self.local
        if local is not None:
            try:
                if isinstance(local, wsmod.WSConnection):
                    local.closed = True
                    local.sock.shutdown(socket.SHUT_RDWR)
                    local.sock.close()
                else:
                    local.shutdown(socket.SHUT_RDWR)
                    local.close()
            except OSError:
                pass
        self.ch.forget(self)

    # --------------------------------------------------------------------- stream threads
    def pull(self):
        """Next item from the viewer (blocks); None when the stream is done."""
        with self.cv:
            while not self.inq and not self.done:
                self.cv.wait()
            if not self.inq:
                return None
            item = self.inq.popleft()
            if isinstance(item, tuple) and item[0] == "data":
                self.queued -= len(item[2])
            return item

    def consumed(self, n):
        """n bytes from the viewer reached the local side: acknowledge every WINDOW/4."""
        with self.cv:
            self.unacked += n
            if self.unacked < e2e.WINDOW // 4:
                return
            n, self.unacked = self.unacked, 0
        self.send_json({"t": "ack", "n": n}, control=True)

    def send_json(self, obj, control=False):
        self.ch.send(e2e.K_JSON, self.sid, json.dumps(obj, separators=(",", ":")).encode(), control=control)

    def send_data(self, data, flags=0):
        """DATA to the viewer in CHUNK pieces; waits while WINDOW bytes are unacknowledged."""
        view = memoryview(data)
        n = len(view)
        for i in range(0, max(n, 1), e2e.CHUNK):
            piece = bytes(view[i:i + e2e.CHUNK])
            with self.cv:
                while self.inflight >= e2e.WINDOW and not self.done and not self.ch.closed:
                    self.cv.wait(1)
                if self.done or self.ch.closed:
                    raise Closed()
                self.inflight += len(piece)
            last = i + e2e.CHUNK >= n
            self.ch.send(e2e.K_DATA, self.sid, piece, flags if last else flags & ~e2e.F_FIN)

    def fail(self, error):
        """End the stream with an error to the viewer (if it is still there)."""
        try:
            self.send_json({"t": "error", "error": str(error)}, control=True)
        except Closed:
            pass
        self.abort()

    def finish(self):
        with self.cv:
            self.done = True
            self.cv.notify_all()
        self.ch.forget(self)

    # ---------------------------------------------------------------------- the work
    def run(self):
        try:
            getattr(self, "_run_" + self.kind)()
        except StreamError as e:
            if not self.done:
                self.fail(e)
        except Closed:
            pass
        except (OSError, wsmod.WSClosed, http.client.HTTPException) as e:
            if not self.done and not self.ch.closed:
                self.fail("port %s: %s" % (self.port, _describe(e)))
        except Exception as e:                      # never kill the channel for one stream
            log.exception("relay stream %s crashed", self.kind)
            if not self.done:
                self.fail("internal error: %s" % e)
        finally:
            self.finish()
            local = self.local
            if local is not None:
                try:
                    local.close()
                except OSError:
                    pass

    # ports
    def _run_ports(self):
        try:
            ports = listening_ports(self.ch.allow())
        except Exception as e:
            raise StreamError("cannot list the ports: %s" % e)
        self.send_json({"t": "ports", "ports": ports})

    # http
    def _run_http(self):
        o = self.opener
        method = o.get("method") or "GET"
        path = o.get("path") or "/"
        if not isinstance(method, str) or not _METHOD_RE.match(method):
            raise StreamError("bad HTTP method")
        if not isinstance(path, str) or not _PATH_RE.match(path):
            raise StreamError("bad path (it must start with /)")
        has_body = bool(o.get("body"))
        headers = request_headers(o.get("headers"), self.port, self.ch.addr)
        clen = [v for k, v in headers if k.lower() == "content-length"]
        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
        sock = local_connect(self.port)
        self.local = sock
        if self.done:
            raise Closed()
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.sock = sock
        first, chunked, ended = b"", False, True
        if has_body and clen and clen[0].isdigit():
            headers.append(("Content-Length", clen[0]))
            ended = False
        elif has_body:                               # no length: buffer a small body, stream a big one
            buf = bytearray()
            ended = False
            while len(buf) < BODY_BUFFER:
                item = self.pull()
                if item is None:
                    raise Closed()
                if item is END:
                    ended = True
                    break
                if item[0] == "data":
                    buf += item[2]
                    self.consumed(len(item[2]))
            first = bytes(buf)
            if ended:
                headers.append(("Content-Length", str(len(first))))
            else:
                headers.append(("Transfer-Encoding", "chunked"))
                chunked = True
        elif method.upper() in ("POST", "PUT", "PATCH"):
            headers.append(("Content-Length", "0"))
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in headers:
                conn.putheader(k, v)
        except ValueError as e:
            raise StreamError("bad request: %s" % e)
        conn.endheaders()

        def write(data):
            if data:
                sock.sendall(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)

        if has_body:
            write(first)
            while not ended:
                item = self.pull()
                if item is None:
                    raise Closed()
                if item is END:
                    break
                if item[0] == "data":
                    write(item[2])
                    self.consumed(len(item[2]))
            if chunked:
                sock.sendall(b"0\r\n\r\n")
        try:
            resp = conn.getresponse()
        except http.client.RemoteDisconnected:
            raise StreamError("port %d closed the connection without an answer" % self.port)
        except http.client.BadStatusLine:
            raise StreamError("port %d does not answer HTTP" % self.port)
        self.send_json({"t": "res", "status": resp.status, "reason": resp.reason,
                        "headers": response_headers(resp.getheaders())})
        read = getattr(resp, "read1", resp.read)
        while True:
            data = read(e2e.CHUNK)
            if not data:
                break
            self.send_data(data)
        self.send_json({"t": "end"})
        conn.close()

    # ws
    def _run_ws(self):
        o = self.opener
        path = o.get("path") or "/"
        if not isinstance(path, str) or not _PATH_RE.match(path):
            raise StreamError("bad path (it must start with /)")
        conn, protocol = local_websocket(self.port, path, o.get("headers"), o.get("protocols"), self.ch.addr)
        self.local = conn
        self.viewer_closed = False
        if self.done:
            raise Closed()
        self.send_json({"t": "open", "protocol": protocol})
        threading.Thread(target=self._ws_up, args=(conn,), name="relay-ws-up", daemon=True).start()
        try:
            while True:
                op, data = conn.recv_message()
                flags = e2e.F_FIN | (e2e.F_TEXT if op == wsmod.OP_TEXT else 0)
                self.send_data(data, flags)
        except (wsmod.WSClosed, OSError):
            if self.done or self.ch.closed or self.viewer_closed:
                return                       # the viewer closed it: nothing to report back
            code = conn.close_code if conn.close_code not in (None, 1005) else (1000 if conn.close_code else 1006)
            reason = conn.close_reason if conn.close_code is not None else "the local WebSocket closed"
            self.send_json({"t": "close", "code": code, "reason": reason}, control=True)
            conn.close_with(code, reason)

    def _ws_up(self, conn):
        """Viewer -> local: whole messages (DATA until FIN), close."""
        parts, text = [], False
        try:
            while True:
                item = self.pull()
                if item is None:
                    return
                if item is END:
                    continue
                if item[0] == "close":
                    self.viewer_closed = True
                    conn.close_with(item[1], item[2])
                    self.finish()
                    return
                _, flags, data = item
                if not parts:
                    text = bool(flags & e2e.F_TEXT)
                parts.append(data)
                if flags & e2e.F_FIN:
                    msg = b"".join(parts)
                    parts = []
                    if text:
                        conn._send(wsmod.OP_TEXT, msg)
                    else:
                        conn.send_binary(msg)
                    self.consumed(len(msg))
        except (wsmod.WSClosed, OSError, Closed):
            pass

    # tcp
    def _run_tcp(self):
        sock = local_connect(self.port)
        self.local = sock
        if self.done:
            raise Closed()
        self.send_json({"t": "open"})
        threading.Thread(target=self._tcp_up, args=(sock,), name="relay-tcp-up", daemon=True).start()
        try:
            while True:
                data = sock.recv(e2e.CHUNK)
                if not data:
                    break
                self.send_data(data)
        except OSError as e:
            if self.done or self.ch.closed:
                return
            self.send_json({"t": "close", "reason": _describe(e)}, control=True)
            return
        self.send_json({"t": "end"})
        with self.cv:
            self.sent_end = True
            while not self.got_end and not self.done and not self.ch.closed:
                self.cv.wait(1)

    def _tcp_up(self, sock):
        try:
            while True:
                item = self.pull()
                if item is None:
                    return
                if item is END:
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    with self.cv:
                        self.got_end = True
                        self.cv.notify_all()
                    return
                if item[0] == "close":              # abort
                    self.abort()
                    return
                sock.sendall(item[2])
                self.consumed(len(item[2]))
        except (OSError, Closed):
            pass


# ------------------------------------------------------------------------- channels
class Channel:
    """One viewer: a data connection through the relay, the handshake, then streams."""

    def __init__(self, agent, n, sid, addr):
        self.agent = agent
        self.n = n
        self.sid = sid
        self.addr = addr
        self.conn = None
        self.secure = None
        self.ready = False
        self.closed = False
        self.reason = None
        self.since = time.time()
        self.bytes_in = 0
        self.bytes_out = 0
        self.streams = {}
        self.max_sid = 0
        self._lock = threading.Lock()          # streams / closed
        self._send_lock = threading.Lock()     # seal + send: nonce order = wire order
        self._qcv = threading.Condition()
        self._q = collections.deque()
        self._qbytes = 0

    def describe(self):
        with self._lock:
            streams = len(self.streams)
        return {"n": self.n, "addr": self.addr, "since": int(self.since), "streams": streams,
                "bytes_in": self.bytes_in, "bytes_out": self.bytes_out}

    def allow(self):
        return self.agent.allow()

    # ---------------------------------------------------------------------- sending
    def send(self, kind, sid, payload, flags=0, control=False):
        """Queue a frame for the writer. control=True never waits (the reader uses it)."""
        with self._qcv:
            if not control:
                while self._qbytes > QUEUE_MAX and not self.closed:
                    self._qcv.wait(1)
            if self.closed:
                raise Closed()
            self._q.append((kind, sid, payload, flags))
            self._qbytes += len(payload)
            self._qcv.notify_all()

    def _writer(self):
        try:
            while True:
                with self._qcv:
                    while not self._q and not self.closed:
                        self._qcv.wait()
                    if self.closed:
                        return
                    kind, sid, payload, flags = self._q.popleft()
                with self._send_lock:
                    ct = self.secure.seal(kind, sid, payload, flags)
                    self.conn.send_binary(ct)
                self.bytes_out += len(ct)
                with self._qcv:
                    self._qbytes -= len(payload)
                    self._qcv.notify_all()
        except (OSError, wsmod.WSClosed) as e:
            self.close("connection lost (%s)" % _describe(e))

    # ------------------------------------------------------------------------ life
    def run(self, cfg, conn=None):
        """Data connection -> linked -> handshake -> frames. Returns when the channel ends.
        conn: an already linked WSConnection (tests); else the data connection is dialled."""
        try:
            if conn is None:
                self._link(cfg)
            else:
                self.conn = conn
            self._handshake(cfg)
        except e2e.WrongKey:
            self.close("wrong key", quiet=True)
            return
        except Closed:
            self.close("closed", quiet=True)
            return
        except (e2e.ProtocolError, TunnelError, OSError, wsmod.WSClosed, ValueError) as e:
            if not self.closed:
                log.info("viewer %d from %s: no channel (%s)", self.n, self.addr,
                         _describe(e) if isinstance(e, OSError) else e)
            self.close(str(e), quiet=True)
            return
        self.ready = True
        log.info("viewer %d connected from %s", self.n, self.addr)
        self.agent.changed()
        threading.Thread(target=self._writer, name="relay-ch%d-tx" % self.n, daemon=True).start()
        reason = "closed by the viewer"
        try:
            self.send(e2e.K_JSON, 0, json.dumps(self.agent.ready_message(), separators=(",", ":")).encode(),
                      control=True)
            self._read_loop()
        except Closed:
            reason = self.reason or "closed"
        except (wsmod.WSClosed, OSError) as e:
            reason = "connection lost (%s)" % _describe(e) if not isinstance(e, wsmod.WSClosed) else "closed by the viewer"
        except e2e.ProtocolError as e:
            reason = "protocol error: %s" % e
        self.close(reason)

    def _link(self, cfg):
        conn = wsmod.connect(ws_base(cfg["url"]) + "/api/agent/data", timeout=CONTROL_TIMEOUT)
        with self._lock:
            if self.closed:
                conn.close()
                raise Closed()
            self.conn = conn
        conn.sock.settimeout(CONTROL_TIMEOUT)
        _send_json(conn, {"t": "data", "v": e2e.VERSION, "id": cfg["id"], "secret": cfg["secret"], "sid": self.sid})
        msg = _recv_json(conn)
        if msg.get("t") == "error":
            raise TunnelError("the device manager refused the data connection: %s" % msg.get("error"))
        if msg.get("t") != "linked":
            raise TunnelError("expected linked, got %s" % msg.get("t"))
        try:                                   # a silently dead path frees the channel in ~2 min
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    conn.sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
        except OSError:
            pass

    def _text(self, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise e2e.ProtocolError("the viewer did not finish the handshake in %d s" % HANDSHAKE_SEC)
        self.conn.sock.settimeout(left)
        try:
            op, data = self.conn.recv_message()
        except socket.timeout:
            raise e2e.ProtocolError("the viewer did not finish the handshake in %d s" % HANDSHAKE_SEC)
        self.bytes_in += len(data)
        if op != wsmod.OP_TEXT:
            raise e2e.ProtocolError("a handshake message must be text")
        try:
            msg = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            raise e2e.ProtocolError("bad handshake message")
        if not isinstance(msg, dict):
            raise e2e.ProtocolError("bad handshake message")
        return msg

    def _say(self, obj):
        text = json.dumps(obj, separators=(",", ":"))
        with self._send_lock:
            self.conn.send_text(text)
        self.bytes_out += len(text)

    def _handshake(self, _cfg):
        deadline = time.monotonic() + HANDSHAKE_SEC
        hello = self._text(deadline)
        # the key of NOW (not of when the control connection started): a replaced key counts at once
        state, answer = e2e.device_answer(self.agent.cfg["key_raw"], hello)
        self._say(answer)
        try:
            proof = self._text(deadline)
        except (OSError, wsmod.WSClosed):
            if self.closed:
                raise Closed()
            # A viewer checks this device's proof first and, with another key, just goes away
            # (it sends nothing derived from its key): that is how a wrong key looks from here.
            self.agent.wrong_key(self.addr, "the viewer did not accept this device's proof")
            raise e2e.WrongKey("the viewer has a different access key")
        try:
            self.secure = e2e.device_check(state, proof)
        except e2e.WrongKey:
            try:
                self._say({"t": "error", "error": "wrong key"})
            except (OSError, wsmod.WSClosed):
                pass
            self.agent.wrong_key(self.addr, "wrong proof")
            raise
        self.conn.sock.settimeout(None)

    def _read_loop(self):
        while True:
            op, data = self.conn.recv_message()
            self.bytes_in += len(data)
            if op != wsmod.OP_BINARY:
                raise e2e.ProtocolError("a text message after the handshake")
            kind, sid, flags, payload = self.secure.open(data)
            if sid == 0:
                if kind == e2e.K_JSON and e2e.decode_json(payload).get("t") == "ping":
                    self.send(e2e.K_JSON, 0, b'{"t":"pong"}', control=True)
                continue
            with self._lock:
                stream = self.streams.get(sid)
            if stream is None:
                if kind == e2e.K_JSON and sid > self.max_sid:
                    self._open_stream(sid, e2e.decode_json(payload))
                continue                        # late frames of a finished stream
            if kind == e2e.K_DATA:
                stream.push(("data", flags, payload))
                continue
            msg = e2e.decode_json(payload)
            t = msg["t"]
            if t == "ack":
                try:
                    stream.acked(int(msg.get("n", 0)))
                except (TypeError, ValueError):
                    pass
            elif t == "end":
                stream.push(END)
            elif t == "close":
                if stream.kind == "ws":
                    code = msg.get("code")
                    stream.push(("close", code if isinstance(code, int) else 1000, str(msg.get("reason") or "")))
                else:
                    stream.abort()
            elif t == "cancel":
                stream.abort()

    def _error(self, sid, error):
        self.send(e2e.K_JSON, sid, json.dumps({"t": "error", "error": error}).encode(), control=True)

    def _open_stream(self, sid, msg):
        self.max_sid = sid
        t = msg.get("t")
        if t not in ("http", "ws", "tcp", "ports"):
            return self._error(sid, "unknown stream type %s" % t)
        if t != "ports":
            port = msg.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                return self._error(sid, "port must be 1-65535")
            allow = self.allow()
            if allow is not None and port not in allow:
                return self._error(sid, "port %d is not allowed on this device" % port)
        with self._lock:
            if self.closed:
                return
            if len(self.streams) >= MAX_STREAMS:
                return self._error(sid, "too many streams")
            stream = Stream(self, sid, msg)
            self.streams[sid] = stream
        threading.Thread(target=stream.run, name="relay-ch%d-s%d" % (self.n, sid), daemon=True).start()

    def forget(self, stream):
        with self._lock:
            if self.streams.get(stream.sid) is stream:
                del self.streams[stream.sid]

    def close(self, reason="closed", quiet=False):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.reason = reason
            streams = list(self.streams.values())
            self.streams.clear()
            conn = self.conn
        with self._qcv:
            self._qcv.notify_all()
        for s in streams:
            with s.cv:
                s.done = True
                s.cv.notify_all()
            s.abort()
        if conn is not None:
            conn.close()
        if self.ready and not quiet:
            log.info("viewer %d (%s) closed after %ds: %d bytes in, %d bytes out (%s)", self.n, self.addr,
                     time.time() - self.since, self.bytes_in, self.bytes_out, reason)
        self.agent.channel_closed(self)


# ------------------------------------------------------------------------- the agent
class Agent:
    """The control connection to the device manager and the viewer channels.

    configure(cfg) with cfg = {url, token, name, enabled, ports, id, secret, key_raw, version,
    hostname, platform, slot, web, blocked}; `on_change()` is called after every change of
    state(), viewers or wrong keys."""

    def __init__(self, on_change=None):
        self.on_change = on_change or (lambda: None)
        self.cfg = {}
        self.state = "off"
        self.error = None
        self.since = time.time()
        self.relay_version = None
        self.channels = {}
        self.wrong_total = 0
        self._wrong = collections.deque()
        self._locked_until = 0.0
        self._refused_locked = 0
        self._numbers = itertools.count(1)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._gen = 0
        self._conn = None
        self._thread = None

    # ------------------------------------------------------------------- settings
    def configure(self, cfg, reconnect=True, close_channels=False):
        with self._lock:
            self.cfg = dict(cfg)
            if reconnect:
                self._gen += 1
                conn = self._conn
            else:
                conn = None
        if close_channels:
            self.close_channels("internet access settings changed")
        if conn is not None:
            conn.close()
        self._wake.set()

    def allow(self):
        ports = self.cfg.get("ports")
        return None if ports is None else set(ports)

    def ready_message(self):
        c = self.cfg
        return {"t": "ready", "v": e2e.VERSION, "id": c.get("id"), "name": c.get("name"),
                "hostname": c.get("hostname"), "version": c.get("version"), "slot": c.get("slot"),
                "ports": c.get("ports")}

    # ---------------------------------------------------------------------- state
    def _set_state(self, state, error=None):
        with self._lock:
            if state == self.state and error == self.error:
                return
            self.state, self.error, self.since = state, error, time.time()
        self.changed()

    def changed(self):
        try:
            self.on_change()
        except Exception:
            log.debug("relay on_change failed", exc_info=True)

    def viewers(self):
        with self._lock:
            chans = [c for c in self.channels.values() if c.ready and not c.closed]
        return [c.describe() for c in sorted(chans, key=lambda c: c.n)]

    def has_viewers(self):
        with self._lock:
            return any(c.ready for c in self.channels.values())

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._run, name="relay-agent", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            conn = self._conn
        if conn is not None:
            conn.close()
        self.close_channels("server stopping")
        if self._thread:
            self._thread.join(5)

    def close_channels(self, reason):
        with self._lock:
            chans = list(self.channels.values())
        for c in chans:
            c.close(reason)
        return len([c for c in chans if c.ready])

    def channel_closed(self, ch):
        with self._lock:
            self.channels.pop(ch.n, None)
        self.changed()

    # ------------------------------------------------------------------ wrong keys
    def wrong_key(self, addr, how="wrong proof"):
        now = time.monotonic()
        with self._lock:
            self.wrong_total += 1
            self._wrong.append(now)
            while self._wrong and self._wrong[0] < now - WRONG_WINDOW:
                self._wrong.popleft()
            recent = len(self._wrong)
            lock = recent > WRONG_MAX and self._locked_until <= now
            if lock:
                self._locked_until = now + LOCKOUT_SEC
                self._refused_locked = 0
        if lock:
            log.warning("%d wrong access keys in %d s (last from %s): refusing new viewers for %d s",
                        recent, WRONG_WINDOW, addr, LOCKOUT_SEC)
        else:
            log.warning("viewer from %s: wrong access key - %s (%d in the last minute)", addr, how, recent)
        self.changed()

    def _locked(self):
        with self._lock:
            if self._locked_until > time.monotonic():
                self._refused_locked += 1
                return True
            if self._refused_locked:
                log.warning("wrong-key lockout over: %d viewer(s) were refused during it", self._refused_locked)
                self._refused_locked = 0
            return False

    # --------------------------------------------------------------------- the loop
    def _active(self, cfg):
        if not cfg.get("enabled"):
            return "off", None
        if cfg.get("blocked"):
            return "error", cfg["blocked"]
        if not cfg.get("url") or not cfg.get("token"):
            return "off", "set the device manager URL and its join token"
        return None, None

    def _run(self):
        backoff = BACKOFF_MIN
        while not self._stop.is_set():
            with self._lock:
                cfg, gen = self.cfg, self._gen
            state, why = self._active(cfg)
            if state:
                self.relay_version = None
                self._set_state(state, why)
                self._wake.wait()
                self._wake.clear()
                backoff = BACKOFF_MIN
                continue
            self._set_state("connecting", self.error if self.state == "error" else None)
            online = [False]
            try:
                self._session(cfg, gen, online)
                err = "the device manager closed the connection"
            except TunnelError as e:
                err = str(e)
            except (OSError, wsmod.WSClosed, ValueError) as e:
                err = _describe(e)
            if self._stop.is_set():
                break
            with self._lock:
                regen = gen != self._gen
            if regen:
                backoff = BACKOFF_MIN
                continue
            if online[0]:
                backoff = BACKOFF_MIN
            self.relay_version = None
            self._set_state("error", err)
            delay = backoff * random.uniform(0.8, 1.2)
            backoff = min(backoff * 2, BACKOFF_MAX)
            log.debug("relay: retrying in %.1f s (%s)", delay, err)
            self._wake.wait(delay)
            self._wake.clear()
        self._set_state("off", None)

    def _session(self, cfg, gen, online):
        url = ws_base(cfg["url"])
        conn = wsmod.connect(url + "/api/agent", timeout=CONTROL_TIMEOUT)
        with self._lock:
            if gen != self._gen or self._stop.is_set():
                conn.close()
                return
            self._conn = conn
        try:
            conn.sock.settimeout(CONTROL_TIMEOUT)
            _send_json(conn, {"t": "hello", "v": e2e.VERSION, "id": cfg["id"], "name": cfg["name"],
                              "secret": cfg["secret"], "token": cfg["token"], "version": cfg.get("version"),
                              "hostname": cfg.get("hostname"), "platform": cfg.get("platform"),
                              "slot": cfg.get("slot"), "web": cfg.get("web")})
            msg = _recv_json(conn)
            if msg.get("t") == "error":
                raise TunnelError("the device manager refused this device: %s" % (msg.get("error") or "no reason"))
            if msg.get("t") != "welcome":
                raise TunnelError("unexpected answer from the device manager: %s" % msg.get("t"))
            try:
                heartbeat = min(300.0, max(0.2, float(msg.get("heartbeat") or HEARTBEAT)))
            except (TypeError, ValueError):
                heartbeat = float(HEARTBEAT)
            self.relay_version = str(msg.get("relay")) if msg.get("relay") is not None else None
            online[0] = True
            log.info("tunnel up: %s as %s (%s), device manager %s", cfg["url"], cfg["id"], cfg["name"],
                     self.relay_version or "?")
            self._set_state("online", None)
            threading.Thread(target=self._pinger, args=(conn, heartbeat), name="relay-ping", daemon=True).start()
            conn.sock.settimeout(3 * heartbeat)
            while True:
                try:
                    msg = _recv_json(conn)
                except socket.timeout:
                    raise TunnelError("no answer from the device manager for %g s" % (3 * heartbeat))
                t = msg.get("t")
                if t == "open":
                    self._open(conn, cfg, msg)
                elif t == "bye":
                    raise TunnelError("the device manager closed the connection: %s" % (msg.get("reason") or "bye"))
                elif t == "error":
                    raise TunnelError("the device manager: %s" % (msg.get("error") or "error"))
        except (OSError, wsmod.WSClosed) as e:
            with self._lock:
                regen = gen != self._gen or self._stop.is_set()
            if regen:
                return
            raise TunnelError("connection lost (%s)" % _describe(e)) if online[0] else e
        finally:
            with self._lock:
                if self._conn is conn:
                    self._conn = None
            conn.close()
            if online[0]:
                log.info("tunnel down: %s", cfg["url"])

    def _pinger(self, conn, heartbeat):
        while not conn.closed and not self._stop.is_set():
            time.sleep(heartbeat)
            try:
                _send_json(conn, {"t": "ping"})
            except (OSError, wsmod.WSClosed):
                return

    def _open(self, conn, cfg, msg):
        sid = msg.get("sid")
        addr = str(msg.get("addr") or "?")[:64]
        if not isinstance(sid, str) or not sid:
            return
        error = None
        if self._locked():
            error = TOO_MANY_WRONG
        else:
            with self._lock:
                if len(self.channels) >= MAX_CHANNELS:
                    error = "too many viewers"
                else:
                    ch = Channel(self, next(self._numbers), sid, addr)
                    self.channels[ch.n] = ch
        if error:
            log.info("viewer from %s refused: %s", addr, error)
            try:
                _send_json(conn, {"t": "refuse", "sid": sid, "error": error})
            except (OSError, wsmod.WSClosed):
                pass
            return
        threading.Thread(target=ch.run, args=(cfg,), name="relay-ch%d" % ch.n, daemon=True).start()
