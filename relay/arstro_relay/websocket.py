"""Minimal asyncio HTTP/1.1 requests and RFC 6455 WebSockets (server side) for the relay.

Every peer of the relay is a WebSocket client (browsers, devices, agents), so incoming
frames must be masked and outgoing ones are not. Messages are reassembled from fragments,
ping is answered with pong, and a message larger than the limit closes the socket with 1009.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import struct
from http import HTTPStatus

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_HEAD = 64 * 1024          # request line + headers
MAX_BODY = 16 * 1024          # request bodies (login, pipe open; pipe up requests get their own limit)
LINGER = 2                    # seconds to wait for the peer's close frame
_tasks = set()                # background close handshakes (kept referenced until done)

# Headers on every relay response (docs/relay/PROTOCOL.md section 1).
COMMON_HEADERS = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer")]


class BadRequest(Exception):
    """Answer the request with this HTTP status and message."""

    def __init__(self, status, message, headers=()):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, list(headers)


class Request:
    def __init__(self, method, target, version, headers):
        self.method, self.target, self.version, self.headers = method, target, version, headers
        self.path, _, self.query = target.partition("?")
        self.body = b""
        self.addr = None      # client address (set by the server)
        self.https = False
        self.pipe = None      # B/api/pipe/open: the route's handler gets a pipe instead of a WebSocket

    def header(self, name, default=""):
        return self.headers.get(name, default)

    @property
    def keep_alive(self):
        conn = [t.strip() for t in self.header("connection").lower().split(",")]
        return self.version == "HTTP/1.1" and "close" not in conn


async def read_request(reader, timeout, body_limit=None):
    """The next request on a connection, or None when the client closed it or went idle.
    body_limit(req) -> the largest body for that request (default MAX_BODY)."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        return None
    except asyncio.LimitOverrunError:
        raise BadRequest(431, "the request headers are too large")
    lines = head.decode("latin-1").lstrip("\r\n").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] not in ("HTTP/1.0", "HTTP/1.1") or not parts[1].startswith("/"):
        raise BadRequest(400, "bad request line")
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        k, sep, v = line.partition(":")
        if not sep or not k or k != k.strip() or line[0] in " \t":
            raise BadRequest(400, "bad header line")
        k, v = k.lower(), v.strip()
        headers[k] = headers[k] + ", " + v if k in headers else v
    req = Request(parts[0], parts[1], parts[2], headers)
    if "transfer-encoding" in headers:
        raise BadRequest(411, "send the body with a Content-Length")
    try:
        n = int(headers.get("content-length", "0"))
    except ValueError:
        raise BadRequest(400, "bad Content-Length")
    if n < 0 or n > (body_limit(req) if body_limit else MAX_BODY):
        raise BadRequest(413, "the request body is too large")
    if n:
        try:
            req.body = await asyncio.wait_for(reader.readexactly(n), timeout)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return None
    return req


async def respond(writer, status, headers=(), body=b"", head=False, close=False):
    """Write one HTTP response (the common security headers are always added)."""
    lines = ["HTTP/1.1 %d %s" % (status, HTTPStatus(status).phrase)]
    hs = COMMON_HEADERS + list(headers)
    if status not in (204, 304):
        hs.append(("Content-Length", str(len(body))))
    if close:
        hs.append(("Connection", "close"))
    lines += ["%s: %s" % kv for kv in hs]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    if body and not head and status not in (204, 304):
        writer.write(body)
    await writer.drain()


def handshake(req):
    """-> the Sec-WebSocket-Accept value for an upgrade request; BadRequest if it is not one."""
    upgrade = req.header("upgrade").lower()
    conn = [t.strip() for t in req.header("connection").lower().split(",")]
    if req.method != "GET" or "websocket" not in upgrade or "upgrade" not in conn:
        raise BadRequest(426, "this address takes WebSocket connections only",
                         [("Upgrade", "websocket"), ("Connection", "Upgrade")])
    if req.header("sec-websocket-version") != "13":
        raise BadRequest(426, "WebSocket version 13 is required", [("Sec-WebSocket-Version", "13")])
    key = req.header("sec-websocket-key")
    try:
        ok = len(base64.b64decode(key, validate=True)) == 16
    except (binascii.Error, ValueError):
        ok = False
    if not ok:
        raise BadRequest(400, "bad Sec-WebSocket-Key")
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


async def accept(req, reader, writer, max_message):
    """Answer an upgrade request with 101 -> WebSocket."""
    key = handshake(req)
    lines = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
             "Sec-WebSocket-Accept: " + key] + ["%s: %s" % kv for kv in COMMON_HEADERS]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    return WebSocket(reader, writer, max_message)


class Closed(Exception):
    """The WebSocket ended. code: the peer's close code (1005 none given, 1006 connection lost)."""

    def __init__(self, code=1006, reason=""):
        super().__init__("closed (%d) %s" % (code, reason))
        self.code, self.reason = code, reason


class _Fail(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def _unmask(data, mask):
    n = len(data)
    if not n:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


def sendable(code):
    """A close code that may be sent on: 1005 / 1006 / 1015 and invalid ones are mapped."""
    if code == 1005:
        return 1000
    if code in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) or 3000 <= code <= 4999:
        return code
    return 1001


class WebSocket:
    """One server-side WebSocket. One task reads (recv), any task may send."""

    transport = "ws"

    def __init__(self, reader, writer, max_message):
        self.r, self.w = reader, writer
        self.max_message = max_message
        self._lock = asyncio.Lock()
        self._reading = False
        self._discard = False             # closing: frames are skipped until the peer's close
        self._skip = 0                    # bytes of a refused frame still to skip
        self._done = asyncio.Event()      # the peer's close frame arrived, or the connection ended
        self.close_sent = False

    # ------------------------------------------------------------------ receive
    async def _frame(self, size):
        b0, b1 = await self.r.readexactly(2)
        fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", await self.r.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await self.r.readexactly(8))[0]
        error = None
        if not masked:
            error = 1002, "frames from a client must be masked"
        elif b0 & 0x70:
            error = 1002, "reserved bits set"
        elif op >= 0x8:
            if op not in (OP_CLOSE, OP_PING, OP_PONG) or n > 125 or not fin:
                error = 1002, "bad control frame"
        elif op not in (OP_CONT, OP_TEXT, OP_BINARY):
            error = 1002, "unknown opcode"
        elif size + n > self.max_message:
            error = 1009, "message too large (the limit is %d bytes)" % self.max_message
        if error or self._discard:
            self._skip = n + (4 if masked else 0)       # the rest of this frame is not read
            if not self._discard:
                raise _Fail(*error)
            await self._skip_rest()
            return fin, op, b""
        mask = await self.r.readexactly(4)
        return fin, op, _unmask(await self.r.readexactly(n), mask)

    async def _skip_rest(self):
        while self._skip:
            self._skip -= len(await self.r.readexactly(min(self._skip, 65536)))

    async def recv(self):
        """-> (OP_TEXT | OP_BINARY, payload) of the next message. Raises Closed at the end."""
        self._reading = True
        parts, op, size = [], None, 0
        try:
            while True:
                fin, opcode, data = await self._frame(size)
                if opcode == OP_PING:
                    try:
                        await self._send(OP_PONG, data)
                    except Closed:          # we are closing: the peer's close follows
                        pass
                elif opcode == OP_PONG:
                    pass
                elif opcode == OP_CLOSE:
                    code, reason = 1005, ""
                    if len(data) == 1:
                        code = 1002
                    elif len(data) >= 2:
                        code = struct.unpack(">H", data[:2])[0]
                        reason = data[2:].decode("utf-8", "replace")
                    self._done.set()
                    await self._send_close(sendable(code))
                    self.abort()
                    raise Closed(code, reason)
                else:
                    if opcode == OP_CONT and op is None:
                        raise _Fail(1002, "continuation frame without a message")
                    if opcode != OP_CONT and op is not None:
                        raise _Fail(1002, "a new message started before the last one ended")
                    op = op if opcode == OP_CONT else opcode
                    parts.append(data)
                    size += len(data)
                    if fin:
                        payload = b"".join(parts)
                        if op == OP_TEXT:
                            try:
                                payload.decode("utf-8")
                            except UnicodeDecodeError:
                                raise _Fail(1007, "a text message must be UTF-8")
                        return op, payload
        except _Fail as e:
            self._reading = False
            await self._send_close(e.code, e.reason)
            task = asyncio.ensure_future(self._linger())
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
            raise Closed(e.code, e.reason)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self._done.set()
            self.abort()
            raise Closed(1006, "connection lost")
        finally:
            self._reading = False

    async def recv_json(self, timeout):
        """The next message as a JSON object (text), or None if it is not one. Raises Closed / TimeoutError."""
        op, data = await asyncio.wait_for(self.recv(), timeout)
        if op != OP_TEXT:
            return None
        try:
            msg = json.loads(data)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None

    # --------------------------------------------------------------------- send
    async def _send(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, n)
        elif n < 1 << 16:
            head = struct.pack(">BBH", 0x80 | opcode, 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
        async with self._lock:
            if self.w.is_closing() or (self.close_sent and opcode != OP_CLOSE):
                raise Closed(1006, "connection closed")
            try:
                self.w.writelines([head, payload])
                await self.w.drain()      # backpressure: wait until the peer takes it
            except (ConnectionError, OSError):
                raise Closed(1006, "connection lost")

    async def send(self, opcode, payload):
        await self._send(opcode, payload)

    async def send_json(self, obj):
        await self._send(OP_TEXT, json.dumps(obj, separators=(",", ":")).encode())

    async def _send_close(self, code, reason=""):
        if self.close_sent:
            return
        self.close_sent = True
        try:
            await self._send(OP_CLOSE, struct.pack(">H", code) + reason.encode()[:120])
        except Closed:
            pass

    # -------------------------------------------------------------------- close
    async def _linger(self):
        """Nobody reads any more: wait (briefly) for the peer's close frame, then drop the connection."""
        self._discard = True

        async def until_close():
            await self._skip_rest()
            while True:
                _, op, _ = await self._frame(0)
                if op == OP_CLOSE:
                    return
        try:
            await asyncio.wait_for(until_close(), LINGER)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        self._done.set()
        self.abort()

    async def close(self, code=1000, reason=""):
        """Close handshake: send close, wait for the peer's close (at most LINGER s), drop the connection."""
        first = not self.close_sent
        await self._send_close(code, reason)
        if self._reading or not first:
            try:
                await asyncio.wait_for(self._done.wait(), LINGER)
            except asyncio.TimeoutError:
                pass
            self.abort()
        else:
            await self._linger()

    def abort(self):
        try:
            self.w.close()
        except Exception:
            pass
