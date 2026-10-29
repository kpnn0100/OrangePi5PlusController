"""The HTTP transport ("pipes", docs/relay/PROTOCOL.md section 6): a WebSocket's messages over plain requests.

For proxies that do not pass WebSockets. A `Pipe` has the interface of websocket.WebSocket
(recv, recv_json, send, send_json, close, abort; ends with Closed), so the relay's handlers
run on it unchanged. The client drives it with two requests:

  GET  B/api/pipe/<id>/down?ack=n   -> the queued messages from sequence number n on
                                       (records kind u8 | seq u32 | length u32 | payload),
                                       or 204 after the poll time without one
  POST B/api/pipe/<id>/up?seq=n     <- records kind u8 | length u32 | payload; record i is the
                                       (n+i)-th up message: applied once, in order -> {"next"}

Down messages stay queued until the client acknowledges them (a lost answer is simply asked
for again); the sender waits while more than PIPE_BUFFER bytes are queued in its direction;
a close record (kind 8) ends the pipe; a pipe without requests for PIPE_IDLE seconds ends
like a lost connection (1006).
"""

import asyncio
import contextlib
import json
import secrets
import struct
import time
from collections import deque

from .websocket import OP_BINARY, OP_TEXT, Closed, sendable

# = e2e.PIPE_* (relay/tests check that they agree; this package does not import e2e.py)
PIPE_MAX_UP = 256 * 1024        # largest up body
PIPE_POLL = 25                  # s a down request waits for a message, then 204
PIPE_IDLE = 60                  # s without any request: the pipe is dropped (1006)
PIPE_BUFFER = 2 << 20           # queued bytes per direction before the sender waits
PIPE_SLACK = 4096               # accepted above PIPE_MAX_UP (record headers of a message that alone is PIPE_MAX_UP)
DOWN_MAX = 1 << 20              # largest down answer (it always carries at least one whole message)
PIPES_PER_ADDRESS = 256         # open pipes per client address (a device: 1 control + up to 32 data)
MAX_PIPES = 20000
LINGER = 2                      # s close() waits for its close record to be fetched (like a WebSocket's close)
COALESCE = 0.002                # s a down answer waits for more messages once it has one
CLOSE_GRACE = 10                # s a pipe stays after its close record was fetched (the answer may be lost: asked again)
K_TEXT, K_BINARY, K_CLOSE = 1, 2, 8
_END = None                     # inbox entry: the client's close (or a broken message) - recv raises Closed


def _reason(text, n=120):
    """A close reason as at most n bytes of UTF-8 (never a cut character)."""
    return text.encode("utf-8")[:n].decode("utf-8", "ignore").encode("utf-8")


def parse_up(body):
    """Up body -> [(kind, payload)]. ValueError if it is broken."""
    out, i = [], 0
    while i < len(body):
        if len(body) - i < 5:
            raise ValueError("a record header is cut off")
        kind, n = struct.unpack_from(">BI", body, i)
        i += 5
        if kind not in (K_TEXT, K_BINARY, K_CLOSE):
            raise ValueError("unknown record kind %d" % kind)
        if len(body) - i < n:
            raise ValueError("a record is cut off")
        out.append((kind, body[i:i + n]))
        i += n
    return out


class Pipe:
    """One pipe: a WebSocket-like object for the handlers, plus the down / up requests' side."""

    transport = "http"

    def __init__(self, path, addr, poll, idle, on_end):
        self.id = secrets.token_urlsafe(16)          # 128 bits: the pipe's only credential
        self.path, self.addr = path, addr
        self.poll, self.idle = poll, idle
        self._on_end = on_end                        # removes the pipe from the registry
        self.t0 = self.seen = time.monotonic()
        self.busy = 0                                # requests in progress (a waiting down request counts)
        # down: relay -> client
        self.out = deque()                           # (seq, kind, payload), not acknowledged yet
        self.out_seq = 0                             # seq of the next down message
        self.out_bytes = 0
        self.close_sent = False                      # a close record is queued: nothing more goes down
        self.close_seq = None
        self.close_code, self.close_reason = 1000, ""
        self.close_delivered = None                  # when an answer carrying the close record was written
        self._poller = None                          # future of the down request now waiting
        # up: client -> relay
        self.inbox = deque()                         # (op, payload) for recv(); (_END, info) ends it
        self.in_bytes = 0
        self.up_count = 0                            # up records applied (= the next expected seq)
        self.up_done = False                         # the client's close was applied: later records are dropped
        self.ended = None                            # Closed when the pipe was dropped (expired / aborted)
        self._event = asyncio.Event()

    @property
    def short(self):
        """The id for logs (never the whole id)."""
        return self.id[:6] + "..."

    def _changed(self):
        self._event.set()
        self._event = asyncio.Event()

    async def _wait(self, pred, timeout=None):
        """Wait until pred() is true -> True, or the timeout passed -> pred()."""
        end = None if timeout is None else time.monotonic() + timeout
        while not pred():
            ev = self._event
            if end is None:
                await ev.wait()
                continue
            left = end - time.monotonic()
            if left <= 0:
                return pred()
            try:
                await asyncio.wait_for(ev.wait(), left)
            except asyncio.TimeoutError:
                return pred()
        return True

    # ------------------------------------------------- the WebSocket interface
    async def recv(self):
        """-> (OP_TEXT | OP_BINARY, payload) of the next up message. Raises Closed at the end."""
        await self._wait(lambda: self.inbox or self.ended or self.close_sent)
        if self.inbox:
            op, payload = self.inbox.popleft()
            if op is _END:
                code, reason, echo = payload
                self._queue_close(*echo)             # like a WebSocket answering the peer's close
                raise Closed(code, reason)
            self.in_bytes -= len(payload)
            self._changed()
            return op, payload
        if self.ended:
            raise Closed(self.ended.code, self.ended.reason)
        raise Closed(self.close_code, self.close_reason)    # we closed it: a pipe client sends no answer

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

    async def send(self, opcode, payload):
        """Queue a message for the client; waits while more than PIPE_BUFFER bytes are unacknowledged."""
        await self._wait(lambda: self.out_bytes <= PIPE_BUFFER or self.close_sent or self.ended)
        if self.close_sent or self.ended:
            raise Closed(1006, "connection closed")
        self._push(K_TEXT if opcode == OP_TEXT else K_BINARY, bytes(payload))

    async def send_json(self, obj):
        await self.send(OP_TEXT, json.dumps(obj, separators=(",", ":")).encode())

    async def close(self, code=1000, reason=""):
        """Queue a close record; wait (at most LINGER s) until the client fetched it."""
        self._queue_close(code, reason)
        await self._wait(lambda: self.close_delivered or self.ended, LINGER)

    def abort(self):
        """Drop the pipe like a lost connection - unless a close is already on its way to the client."""
        if not self.close_sent:
            self.drop(Closed(1006, "connection lost"))

    def _push(self, kind, payload):
        self.out.append((self.out_seq, kind, payload))
        self.out_seq += 1
        self.out_bytes += len(payload)
        self._changed()

    def _queue_close(self, code, reason):
        if self.close_sent or self.ended:
            return
        self.close_sent = True
        self.close_code, self.close_reason = code, reason
        self.close_seq = self.out_seq
        self._push(K_CLOSE, struct.pack(">H", code) + _reason(reason))

    def drop(self, exc):
        """The pipe is gone (expired, aborted, or its close was acknowledged): requests get 410."""
        if self.ended:
            return
        self.ended = exc
        self._changed()
        self._on_end(self)

    # ------------------------------------------------------- the client's side
    @contextlib.contextmanager
    def request(self):
        """Around each down / up request (until its answer is written): the client is alive."""
        self.busy += 1
        self.seen = time.monotonic()
        try:
            yield self
        finally:
            self.busy -= 1
            self.seen = time.monotonic()

    def _forget(self, ack):
        while self.out and self.out[0][0] < ack:
            self.out_bytes -= len(self.out.popleft()[2])
        self._changed()

    async def down(self, ack):
        """GET .../down?ack=<ack> (inside request()) -> (status, body, last seq in the body)."""
        if ack > self.out_seq:
            return 400, b"", None
        self._forget(ack)
        if self.ended:
            return 410, b"", None
        if self.close_seq is not None and ack > self.close_seq:
            self.drop(Closed(self.close_code, self.close_reason))     # the client has the close: done
            return 410, b"", None
        if self._poller is not None and not self._poller.done():
            self._poller.set_result(True)            # a newer down request ends the older one (204)
        me = self._poller = asyncio.get_running_loop().create_future()
        self._changed()
        await self._wait(lambda: me.done() or self.out or self.ended, self.poll)
        if self.out and not me.done():
            await asyncio.sleep(COALESCE)            # messages queued right after go in the same answer
        if self._poller is me:
            self._poller = None
        if me.done():
            return 204, b"", None
        if self.out:
            parts, size, last = [], 0, None
            for seq, kind, payload in self.out:
                if parts and size + 9 + len(payload) > DOWN_MAX:
                    break
                parts += [struct.pack(">BII", kind, seq & 0xFFFFFFFF, len(payload)), payload]
                size += 9 + len(payload)
                last = seq
            return 200, b"".join(parts), last
        return (410 if self.ended else 204), b"", None

    def delivered(self, last):
        """An answer up to seq `last` was written."""
        if last is not None and self.close_seq is not None and last >= self.close_seq and not self.close_delivered:
            self.close_delivered = time.monotonic()
            self._changed()

    async def up(self, seq, body):
        """POST .../up?seq=<seq> (inside request()) -> (status, JSON answer)."""
        if self.ended:
            return 410, {"error": "this pipe is closed"}
        try:
            records = parse_up(body)
        except ValueError as e:
            return 400, {"error": "bad up body: %s" % e}
        if seq > self.up_count:
            return 409, {"next": self.up_count}
        for i, (kind, payload) in enumerate(records):
            if seq + i < self.up_count:
                continue                             # applied before (a retried request)
            self.up_count += 1
            if not (self.up_done or self.close_sent):    # after a close nothing more is taken
                self._apply(kind, payload)
        self._changed()
        # backpressure: hold the answer while too much waits to be forwarded (answers end within the poll time)
        await self._wait(lambda: self.in_bytes <= PIPE_BUFFER or self.ended or self.close_sent, self.poll)
        return 200, {"next": self.up_count}

    def _apply(self, kind, payload):
        if kind == K_CLOSE:
            if len(payload) == 1:
                code, reason = 1002, ""
            elif len(payload) >= 2:
                code, reason = struct.unpack(">H", payload[:2])[0], payload[2:].decode("utf-8", "replace")
            else:
                code, reason = 1005, ""
            self._end(code, reason, (sendable(code), ""))
            return
        if kind == K_TEXT:
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError:
                error = "a text message must be UTF-8"
                self._end(1007, error, (1007, error))
                return
        self.inbox.append((OP_TEXT if kind == K_TEXT else OP_BINARY, payload))
        self.in_bytes += len(payload)

    def _end(self, code, reason, echo):
        self.up_done = True
        self.inbox.append((_END, (code, reason, echo)))

    def expired(self, now):
        if self.busy:
            return False
        return now - self.seen > self.idle or bool(self.close_delivered and now - self.close_delivered > CLOSE_GRACE)


class Pipes:
    """The open pipes by id."""

    def __init__(self, poll=PIPE_POLL, idle=PIPE_IDLE):
        self.poll, self.idle = poll, idle
        self.all = {}

    def __len__(self):
        return len(self.all)

    def count(self, addr):
        return sum(1 for p in self.all.values() if p.addr == addr)

    def open(self, path, addr):
        pipe = Pipe(path, addr, self.poll, self.idle, lambda p: self.all.pop(p.id, None))
        self.all[pipe.id] = pipe
        return pipe

    def get(self, pipe_id):
        return self.all.get(pipe_id)

    def expire(self):
        """Drop pipes without requests for the idle time -> the ones that were still open (for the log)."""
        now, out = time.monotonic(), []
        for pipe in [p for p in self.all.values() if p.expired(now)]:
            if not pipe.close_sent:
                out.append(pipe)
            pipe.drop(Closed(1006, "no request for %g s" % pipe.idle))
        return out
