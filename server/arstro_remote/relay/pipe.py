"""The HTTP transport of the device manager (RLY-27, docs/relay/PROTOCOL.md section 6).

Some reverse proxies do not pass WebSockets (an nginx without the `Upgrade` lines answers the
relay's WebSocket routes with 426). A **pipe** carries exactly the same messages (text and
binary, in order, with a close code) over plain HTTP/1.1 requests:

    POST <base>/api/pipe/open        {"path": "/api/agent"}  -> {"pipe": id, "max_up", "poll"}
    GET  <base>/api/pipe/<id>/down?ack=n   long poll: records  kind u8 | seq u32 | len u32 | payload
    POST <base>/api/pipe/<id>/up?seq=n     batches:   records  kind u8 | len u32 | payload  -> {"next"}

`PipeConnection` has the interface of `ws.WSConnection` that the device agent and the client use
(send_text, send_binary, recv_message, close, closed, settimeout). Two threads per pipe: the
poller (always one down request waiting; re-polls with the same `ack` after a network error, so
nothing is lost or doubled) and the sender (one up request at a time, batches what is queued,
retries with the same `seq`). Each has its own keep-alive HTTP connection.

`connect(url, headers, timeout)` chooses: ARSTRO_RELAY_TRANSPORT=ws|http|auto (default auto).
auto tries the WebSocket first; when that fails before it opens (anything but 401), it opens a
pipe for the same route and keeps using pipes for that device manager while the process runs.

Standard library only, and importable on its own: the standalone client (downloaded from
`<relay>/client/`) carries it next to e2e.py and ws.py. The pipe id is the pipe's only
credential: it is never logged and never part of an error message.
"""

import collections
import http.client
import json
import logging
import os
import re
import socket
import struct
import threading
import time
import urllib.parse

try:
    from . import e2e
    from ..web import ws as wsmod
except ImportError:                      # standalone: e2e.py and ws.py next to this file
    import e2e
    import ws as wsmod

log = logging.getLogger("arstro.relay")

K_TEXT, K_BINARY, K_CLOSE = 1, 2, 8
DOWN_HEAD = struct.Struct(">BII")        # kind | seq | length
UP_HEAD = struct.Struct(">BI")           # kind | length
SLACK = 15                               # s a down request may take beyond the relay's poll time
CLOSE_WAIT = 2.0                         # s close() waits for its close record to be taken
CLOSE_FLUSH = 10.0                       # s the sender keeps trying to deliver after close()
ROUTE_RE = re.compile(r"^(.*?)(/api/(?:agent/data|agent|connect/[^/?#]+))/?$")
PIPE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
MODES = ("ws", "http", "auto")

_lock = threading.Lock()
_use_pipes = set()                       # device manager base URLs where WebSockets did not pass


class PipeRefused(ConnectionError):
    """The device manager (or a proxy in front of it) refused to open a pipe."""

    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


# ---------------------------------------------------------------------------- helpers
def mode():
    m = (os.environ.get("ARSTRO_RELAY_TRANSPORT") or "auto").strip().lower()
    if m not in MODES:
        raise ValueError("ARSTRO_RELAY_TRANSPORT must be ws, http or auto (got %r)" % m)
    return m


def split(url):
    """ws(s)://host/base/api/agent -> ("http(s)://host/base", "/api/agent")."""
    u = urllib.parse.urlsplit(url)
    scheme = {"ws": "http", "http": "http", "wss": "https", "https": "https"}.get(u.scheme)
    if not scheme or not u.hostname:
        raise ValueError("the device manager URL must be http(s)://host/path (got %s)" % u.scheme)
    m = ROUTE_RE.match(u.path)
    if not m:
        raise ValueError("not a device manager connection route: %s" % u.path)
    return urllib.parse.urlunsplit((scheme, u.netloc, m.group(1), "", "")), m.group(2)


def kind(conn):
    """'http' for a pipe, 'ws' for a WebSocket."""
    return "http" if isinstance(conn, PipeConnection) else "ws"


def _http(base, timeout):
    u = urllib.parse.urlsplit(base)
    if u.scheme == "https":
        import ssl
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def _error_text(data):
    try:
        err = json.loads(data.decode()).get("error")
        return str(err)[:200] if err else ""
    except (ValueError, UnicodeDecodeError, AttributeError):
        return ""


def _describe(e):
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


# ---------------------------------------------------------------------------- choosing
def connect(url, headers=None, timeout=10):
    """A connection to a device manager route (ws(s):// or http(s)://<base>/api/agent,
    /api/agent/data, /api/connect/<id>): a WebSocket, or a pipe when WebSockets do not pass.
    Raises like ws.connect: PermissionError on 401, ConnectionError / OSError otherwise."""
    m = mode()
    base, path = split(url)
    if m == "ws":
        return wsmod.connect(url, headers, timeout)
    with _lock:
        remembered = base in _use_pipes
    if m == "http" or remembered:
        return open_pipe(base, path, headers, timeout)
    try:
        return wsmod.connect(url, headers, timeout)
    except PermissionError:
        raise
    except OSError as e:                         # ConnectionError, socket errors, TLS, timeouts
        ws_error = e
    try:
        conn = open_pipe(base, path, headers, timeout)
    except PipeRefused as e:
        if e.status in (404, 405):               # a device manager without the HTTP transport
            raise ConnectionError("%s (and the device manager has no HTTP transport: update it, or let "
                                  "its proxy pass WebSockets)" % _describe(ws_error))
        raise
    with _lock:
        first = base not in _use_pipes
        _use_pipes.add(base)
    if first:
        log.info("the device manager does not take WebSockets here - using HTTP (%s)", _describe(ws_error))
    return conn


def open_pipe(base, path, headers=None, timeout=10):
    """POST <base>/api/pipe/open -> PipeConnection. 401 -> PermissionError, like ws.connect."""
    conn = _http(base, timeout)
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    prefix = urllib.parse.urlsplit(base).path.rstrip("/")
    try:
        conn.request("POST", prefix + "/api/pipe/open", json.dumps({"path": path}).encode(), h)
        resp = conn.getresponse()
        data = resp.read()
    except http.client.HTTPException as e:
        conn.close()
        raise ConnectionError("bad answer from the device manager (%s)" % (_describe(e)))
    except BaseException:
        conn.close()
        raise
    if resp.status != 200:
        conn.close()
        if resp.status == 401:
            raise PermissionError("the server refused the token (401)")
        err = _error_text(data)
        raise PipeRefused(resp.status, "the device manager answered %d %s%s" % (
            resp.status, resp.reason, (": " + err) if err else ""))
    try:
        d = json.loads(data.decode())
        pipe_id = d["pipe"]
        max_up = int(d.get("max_up") or e2e.PIPE_MAX_UP)
        poll = float(d.get("poll") or e2e.PIPE_POLL)
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
        conn.close()
        raise ConnectionError("bad answer from the device manager to opening a pipe")
    if not isinstance(pipe_id, str) or not PIPE_ID_RE.match(pipe_id):
        conn.close()
        raise ConnectionError("bad answer from the device manager to opening a pipe")
    if resp.will_close:
        conn.close()
        conn = None
    return PipeConnection(base, pipe_id, headers, max(4096, min(max_up, 16 << 20)), max(1.0, min(poll, 120.0)),
                          down=conn)


# ---------------------------------------------------------------------------- the pipe
class PipeConnection:
    """One pipe. Same use as a client ws.WSConnection: recv_message() -> (opcode, bytes),
    send_text / send_binary (block while more than PIPE_BUFFER waits), close(code, reason)."""

    transport = "http"

    def __init__(self, base, pipe_id, headers=None, max_up=e2e.PIPE_MAX_UP, poll=e2e.PIPE_POLL, down=None):
        self._base = base
        self._path = urllib.parse.urlsplit(base).path.rstrip("/") + "/api/pipe/" + pipe_id
        self._headers = dict(headers or {})
        self.max_up = max_up
        self.poll = poll
        self.closed = False                  # no more sends (closed here or by the other side, or lost)
        self.close_code = None               # the other side's close code / reason
        self.close_reason = ""
        self._timeout = None                 # recv_message timeout (settimeout)
        self._cv = threading.Condition()
        self._in = collections.deque()       # (opcode, bytes) received, not taken yet
        self._in_bytes = 0
        self._received = 0                   # down messages received = the ack
        self._out = collections.deque()      # (kind, payload) not confirmed by the relay yet
        self._out_bytes = 0
        self._out_base = 0                   # up index of _out[0]
        self._local_closed = False
        self._close_done = threading.Event()
        self._close_deadline = None
        self._eof = False                    # the other side's close record arrived
        self._lost = None                    # why the pipe broke
        self._sender_ok = True
        self._alive = time.monotonic()       # last answer from the device manager
        self.retries = 0                     # requests sent again after a network error (diagnostics, tests)
        self._conns = {"down": down, "up": None}
        threading.Thread(target=self._poller, name="relay-pipe-down", daemon=True).start()
        threading.Thread(target=self._sender, name="relay-pipe-up", daemon=True).start()

    # ------------------------------------------------------------------ WSConnection API
    def settimeout(self, t):
        self._timeout = t

    def recv_message(self):
        """-> (ws.OP_TEXT | ws.OP_BINARY, bytes). Raises ws.WSClosed at the end, socket.timeout
        after settimeout(t) seconds, ConnectionResetError when the pipe broke."""
        with self._cv:
            deadline = None if self._timeout is None else time.monotonic() + self._timeout
            while not self._in:
                if self._local_closed or self._eof or self._lost:
                    break
                if deadline is None:
                    self._cv.wait()
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise socket.timeout("timed out")
                    self._cv.wait(left)
            if self._local_closed:
                raise wsmod.WSClosed("closed")
            if self._in:
                op, data = self._in.popleft()
                self._in_bytes -= len(data)
                self._cv.notify_all()
                return op, data
            if self._eof:
                raise wsmod.WSClosed("closed by peer")
            raise ConnectionResetError(self._lost)

    def send_text(self, text):
        self._send(K_TEXT, text.encode("utf-8"))

    def send_binary(self, data):
        self._send(K_BINARY, bytes(data))

    def _send(self, k, payload):
        if UP_HEAD.size + len(payload) > self.max_up:
            self.close(1009, "message too big")
            raise wsmod.WSClosed("message too large for the HTTP transport (%d bytes)" % len(payload))
        with self._cv:
            while self._out_bytes > e2e.PIPE_BUFFER and not (self.closed or self._lost):
                self._cv.wait(1)
            if self._lost and not self._local_closed:
                raise ConnectionResetError(self._lost)
            if self.closed:
                raise wsmod.WSClosed("closed")
            self._out.append((k, payload))
            self._out_bytes += len(payload)
            self._cv.notify_all()

    def close(self, code=1000, reason=""):
        """Send a close record (unless the other side closed first). Waits up to CLOSE_WAIT s
        for the device manager to take it while the connection is healthy; the poller then
        fetches the relay's answering close in the background (that frees the pipe there)."""
        with self._cv:
            if self._local_closed:
                return
            self._local_closed = True
            send = not (self.closed or self._lost)
            self.closed = True
            self._in.clear()
            self._in_bytes = 0
            if send:
                if not isinstance(code, int) or code in (1005, 1006, 1015) or not 1000 <= code <= 4999:
                    code = 1000
                self._out.append((K_CLOSE, struct.pack(">H", code) + str(reason or "").encode()[:123]))
                self._close_deadline = time.monotonic() + CLOSE_FLUSH
            self._cv.notify_all()
            healthy = self._sender_ok
        if send and healthy:
            self._close_done.wait(CLOSE_WAIT)
        if not send:
            self._abort("down")
            self._abort("up")

    # ------------------------------------------------------------------ HTTP
    def _request(self, which, method, query, body, timeout):
        conn = self._conns.get(which)
        if conn is None:
            conn = _http(self._base, timeout)
            self._conns[which] = conn
        elif conn.timeout != timeout:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
        headers = dict(self._headers)
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        try:
            conn.request(method, self._path + query, body, headers)
            resp = conn.getresponse()
            data = resp.read()
        except BaseException:
            conn.close()
            if self._conns.get(which) is conn:
                self._conns[which] = None
            raise
        if resp.will_close:
            conn.close()
            if self._conns.get(which) is conn:
                self._conns[which] = None
        return resp.status, data

    def _abort(self, which):
        """Break the connection of one thread (unblocks a waiting down request)."""
        conn = self._conns.get(which)
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _expired(self):
        return time.monotonic() - self._alive > e2e.PIPE_IDLE

    def _fail(self, why):
        with self._cv:
            if self._lost is None and not self._eof:
                self._lost = why
            self.closed = True
            self._cv.notify_all()
        self._close_done.set()
        self._abort("down")
        self._abort("up")

    def _pause(self, delay, stop):
        """Wait `delay` s or until stop() is true."""
        end = time.monotonic() + delay
        with self._cv:
            while not stop():
                left = end - time.monotonic()
                if left <= 0:
                    return
                self._cv.wait(left)

    # ------------------------------------------------------------------ down (the poller)
    def _poller(self):
        delay = 0.0

        def down_gone():                         # after close() it waits for the relay's close
            return (self._eof or self._lost is not None or
                    (self._close_deadline is not None and time.monotonic() > self._close_deadline))
        while True:
            with self._cv:
                waited = time.monotonic()        # the reader is slow: let it catch up first
                while (self._in_bytes > e2e.PIPE_BUFFER and not down_gone()
                       and time.monotonic() - waited < e2e.PIPE_IDLE / 2):
                    self._cv.wait(1)
                if down_gone():
                    break
                ack = self._received
            try:
                status, data = self._request("down", "GET", "/down?ack=%d" % ack, None, self.poll + SLACK)
            except (OSError, http.client.HTTPException) as e:
                if down_gone():
                    break
                self.retries += 1
                if self._expired():
                    self._fail("lost the connection to the device manager (%s)" % _describe(e))
                    break
                self._pause(delay, down_gone)
                delay = min(max(delay * 2, 0.25), 5.0)
                continue
            if status == 200:
                self._alive = time.monotonic()
                delay = 0.0
                if not self._deliver(data):
                    break
            elif status == 204:
                self._alive = time.monotonic()
                delay = 0.0
            elif status == 410:
                self._fail("the device manager dropped the connection")
                break
            elif status >= 500 or status in (408, 429):   # a proxy in between had a problem: retry
                if self._expired():
                    self._fail("the device manager did not answer for %d s (HTTP %d)" % (e2e.PIPE_IDLE, status))
                    break
                self._pause(max(delay, 0.5), down_gone)
                delay = min(max(delay * 2, 0.5), 5.0)
            else:
                self._fail("the device manager answered %d%s" % (status, (": " + _error_text(data)) if data else ""))
                break
        if self._eof:                            # tell the relay the close arrived (frees the pipe)
            try:
                with self._cv:
                    ack = self._received
                self._request("down", "GET", "/down?ack=%d" % ack, None, 5)
            except (OSError, http.client.HTTPException):
                pass
        conn = self._conns.get("down")
        if conn is not None:
            conn.close()

    def _deliver(self, body):
        """Records of one down answer -> the receive queue. False when the pipe ended."""
        items, i = [], 0
        while i < len(body):
            if len(body) - i < DOWN_HEAD.size:
                self._fail("bad answer from the device manager (cut record)")
                return False
            k, seq, n = DOWN_HEAD.unpack_from(body, i)
            i += DOWN_HEAD.size
            if len(body) - i < n:
                self._fail("bad answer from the device manager (cut record)")
                return False
            items.append((k, seq, body[i:i + n]))
            i += n
        with self._cv:
            for k, seq, payload in items:
                if seq < self._received:
                    continue                     # sent again after a lost answer: already have it
                if seq > self._received:
                    lost = "messages from the device manager were lost (expected %d, got %d)" % (self._received, seq)
                    break
                self._received += 1
                if k in (K_TEXT, K_BINARY):
                    if not self._local_closed:   # closed here: nobody reads any more
                        self._in.append((wsmod.OP_TEXT if k == K_TEXT else wsmod.OP_BINARY, payload))
                        self._in_bytes += len(payload)
                elif k == K_CLOSE:
                    self.close_code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                    self.close_reason = payload[2:].decode("utf-8", "replace")
                    self._eof = True
                    self.closed = True
                    self._cv.notify_all()
                    break
                else:
                    lost = "bad answer from the device manager (record kind %d)" % k
                    break
            else:
                lost = None
            self._cv.notify_all()
            eof = self._eof
        if eof:
            self._close_done.set()
            self._abort("up")
            return False
        if lost:
            self._fail(lost)
            return False
        return True

    # ------------------------------------------------------------------ up (the sender)
    def _sender(self):
        delay = 0.0
        while True:
            with self._cv:
                while not self._out and not (self._eof or self._lost or self._local_closed):
                    self._cv.wait()
                if self._eof or self._lost or not self._out:
                    break
                if self._close_deadline is not None and time.monotonic() > self._close_deadline:
                    break                        # closed here and the relay does not take it: give up
                seq, parts, size = self._out_base, [], 0
                for k, payload in self._out:
                    rec = UP_HEAD.size + len(payload)
                    if parts and size + rec > self.max_up:
                        break
                    parts.append(UP_HEAD.pack(k, len(payload)))
                    parts.append(payload)
                    size += rec
                body = b"".join(parts)
            stop = lambda: self._eof or self._lost is not None
            try:
                status, data = self._request("up", "POST", "/up?seq=%d" % seq, body, self.poll + SLACK)
            except (OSError, http.client.HTTPException) as e:
                if stop():
                    break
                self.retries += 1
                self._sender_ok = False
                if self._expired():
                    self._fail("lost the connection to the device manager (%s)" % _describe(e))
                    break
                self._pause(delay, stop)
                delay = min(max(delay * 2, 0.25), 5.0)
                continue
            if status in (200, 409):
                self._alive = time.monotonic()
                self._sender_ok = True
                delay = 0.0
                try:
                    nxt = int(json.loads(data.decode())["next"])
                except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
                    self._fail("bad answer from the device manager to sending")
                    break
                with self._cv:
                    drop = nxt - self._out_base
                    if drop < 0 or drop > len(self._out):
                        bad = True
                    else:
                        bad = False
                        for _ in range(drop):
                            k, payload = self._out.popleft()
                            self._out_bytes -= len(payload)
                            if k == K_CLOSE:
                                self._close_done.set()
                        self._out_base = nxt
                        self._cv.notify_all()
                if bad:
                    self._fail("the device manager lost messages (it has %d, this side %d)" % (nxt, self._out_base))
                    break
                if self._close_done.is_set() and self._local_closed:
                    break
            elif status == 410:
                if self._local_closed:
                    self._close_done.set()       # closed here; the relay has let go of it already
                else:
                    self._fail("the device manager dropped the connection")
                break
            elif status >= 500 or status in (408, 429):
                self._sender_ok = False
                if self._expired():
                    self._fail("the device manager did not answer for %d s (HTTP %d)" % (e2e.PIPE_IDLE, status))
                    break
                self._pause(max(delay, 0.5), stop)
                delay = min(max(delay * 2, 0.5), 5.0)
            else:                                # 400 / 413: this side's bug; the pipe cannot go on
                self._fail("the device manager refused a message (%d%s)" % (
                    status, (": " + _error_text(data)) if data else ""))
                break
        self._close_done.set()
        conn = self._conns.get("up")
        if conn is not None:
            conn.close()
