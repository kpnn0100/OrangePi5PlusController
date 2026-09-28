"""Minimal RFC 6455 WebSocket (server and client), no third-party packages.

`WSConnection` sends/receives messages. `WSStream` adapts a connection to the socket
interface `Session` uses (recv / sendall / shutdown / close): the protocol's frames
travel inside binary WebSocket messages, so every op works over WebSocket unchanged.
"""

import base64
import hashlib
import os
import socket
import struct
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 32 << 20


class WSClosed(Exception):
    pass


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class WSConnection:
    """One WebSocket. `read(n)` reads raw bytes (a buffered reader from the HTTP layer
    on the server side, the socket on the client side); writes go to `sock`."""

    def __init__(self, sock, read=None, client=False):
        self.sock = sock
        self._read = read or self._sock_read
        self.client = client
        self._send_lock = threading.Lock()
        self.closed = False

    def _sock_read(self, n):
        return self.sock.recv(n)

    def _exact(self, n):
        out = bytearray()
        while len(out) < n:
            chunk = self._read(n - len(out))
            if not chunk:
                raise WSClosed("connection closed")
            out += chunk
        return bytes(out)

    # -------------------------------------------------------------- receive
    def _frame(self):
        b0, b1 = self._exact(2)
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        masked, length = bool(b1 & 0x80), b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._exact(8))[0]
        if length > MAX_MESSAGE:
            raise WSClosed("message too large")
        mask = self._exact(4) if masked else None
        data = self._exact(length) if length else b""
        if mask:
            data = _xor(data, mask)
        return fin, opcode, data

    def recv_message(self):
        """-> (opcode, payload). Handles ping/pong/close; raises WSClosed at the end."""
        parts, op = [], None
        while True:
            fin, opcode, data = self._frame()
            if opcode == OP_PING:
                self._send(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send(OP_CLOSE, data[:2] or struct.pack(">H", 1000))
                except OSError:
                    pass
                self.closed = True
                raise WSClosed("closed by peer")
            if opcode != OP_CONT:
                op, parts = opcode, [data]
            else:
                parts.append(data)
            if fin:
                return op, b"".join(parts)

    # ----------------------------------------------------------------- send
    def _send(self, opcode, payload):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        mbit = 0x80 if self.client else 0
        if n < 126:
            head.append(mbit | n)
        elif n < 1 << 16:
            head.append(mbit | 126)
            head += struct.pack(">H", n)
        else:
            head.append(mbit | 127)
            head += struct.pack(">Q", n)
        if self.client:
            mask = os.urandom(4)
            head += mask
            payload = _xor(payload, mask)
        with self._send_lock:
            if self.closed and opcode != OP_CLOSE:
                raise WSClosed("closed")
            self.sock.sendall(bytes(head) + payload)

    def send_binary(self, data):
        self._send(OP_BINARY, bytes(data))

    def send_text(self, text):
        self._send(OP_TEXT, text.encode("utf-8"))

    def close(self, code=1000):
        if not self.closed:
            try:
                self._send(OP_CLOSE, struct.pack(">H", code))
            except OSError:
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


def _xor(data, mask):
    # fast path: XOR as big integers in 4-byte-aligned blocks
    n = len(data)
    if not n:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


class WSStream:
    """Socket-like view of a WebSocket for `Session`: binary messages form one byte
    stream of protocol frames. A text message is taken as one JSON op (handy for tools)."""

    def __init__(self, conn):
        self.conn = conn
        self._pending = b""

    def recv(self, n):
        while not self._pending:
            try:
                op, data = self.conn.recv_message()
            except (WSClosed, OSError):
                return b""
            if op == OP_BINARY:
                self._pending = data
            elif op == OP_TEXT and data:
                self._pending = struct.pack(">BI", 0x01, len(data)) + data
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def sendall(self, data):
        try:
            self.conn.send_binary(data)
        except WSClosed as e:
            raise ConnectionError(str(e))

    def shutdown(self, _how):
        self.conn.close()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------- client
def connect(url, headers=None, timeout=10):
    """Open a client WebSocket to ws://host:port/path (http:// is accepted too)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("ws", "http"):
        raise ValueError("only ws:// or http:// URLs are supported (got %s)" % u.scheme)
    host, port = u.hostname, u.port or 80
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = ["GET %s HTTP/1.1" % path, "Host: %s:%d" % (host, port), "Upgrade: websocket",
             "Connection: Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
    for k, v in (headers or {}).items():
        lines.append("%s: %s" % (k, v))
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed the connection during the handshake")
        resp += chunk
        if len(resp) > 65536:
            raise ConnectionError("bad handshake response")
    head, rest = resp.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 101 " not in status + " ":
        code = status.split(" ")[1] if " " in status else "?"
        if code == "401":
            raise PermissionError("the server refused the token (401)")
        raise ConnectionError("WebSocket handshake failed: %s" % status)
    if accept_key(key).encode() not in head:
        raise ConnectionError("bad Sec-WebSocket-Accept")
    sock.settimeout(None)
    buf = [rest]

    def read(n):
        if buf[0]:
            out, buf[0] = buf[0][:n], buf[0][n:]
            return out
        return sock.recv(n)

    return WSConnection(sock, read=read, client=True)
