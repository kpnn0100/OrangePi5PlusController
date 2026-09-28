"""Synchronous protocol client (the CLI controller and the tests use it).

    Client.unix(path)          local control socket (on the Pi, no token)
    Client.ws(url, token)      WebSocket to the web server (from any machine)
"""

import json
import socket
import threading
import time
from collections import deque

from .protocol import T_JSON, T_TERM_OUT, FrameDecoder, decode_term_out, encode_json, encode_term


class Client:
    def __init__(self, sock):
        self.sock = sock
        self._decoder = FrameDecoder()
        self._next_id = 1
        self._responses = {}
        self._ops = {}
        self._cond = threading.Condition()
        self.events = deque(maxlen=10000)
        self.state = {}
        self.term_output = {}
        self.term_base = {}  # stream offset of term_output[tid][0]
        self.on_event = None      # callback(msg) for every event (reader thread)
        self.on_term = None       # callback(term_id, bytes) for new terminal output
        self._closed = False
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()

    @classmethod
    def unix(cls, path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        return cls(s)

    @classmethod
    def ws(cls, url, token, timeout=10):
        from .web import ws as wsmod
        base = url.rstrip("/")
        if base.startswith("https://") or base.startswith("wss://"):
            raise ValueError("https is not supported by the built-in client; use http://")
        if not base.endswith("/ws"):
            base += "/ws"
        conn = wsmod.connect(base, {"Authorization": "Bearer %s" % token} if token else {}, timeout=timeout)
        return cls(wsmod.WSStream(conn))

    @property
    def closed(self):
        return self._closed

    def _reader(self):
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                for ftype, payload in self._decoder.feed(data):
                    self._frame(ftype, payload)
        except OSError:
            pass
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _frame(self, ftype, payload):
        live = None
        with self._cond:
            if ftype == T_JSON:
                msg = json.loads(payload)
                if "id" in msg and "ok" in msg:
                    # A new shell may reuse the id of an exited one: start its buffer
                    # fresh before its output frames arrive.
                    if self._ops.pop(msg["id"], None) == "term.open" and msg.get("ok"):
                        tid = msg["data"]["term"]
                        self.term_output[tid] = bytearray()
                        self.term_base[tid] = 0
                    self._responses[msg["id"]] = msg
                else:
                    if msg.get("ev") == "state":
                        self.state[msg.get("topic")] = msg.get("data")
                    self.events.append(msg)
                    live = ("ev", msg)
            elif ftype == T_TERM_OUT:
                tid, offset, data = decode_term_out(payload)
                buf = self.term_output.setdefault(tid, bytearray())
                have = self.term_base.get(tid, 0) + len(buf)
                if offset > have:  # replay of an older stream start (gap)
                    self.term_base[tid] = offset
                    buf.clear()
                    have = offset
                skip = have - offset
                if skip < len(data):
                    new = data[skip:]
                    buf += new
                    if len(buf) > 4 << 20:            # keep memory bounded for long sessions
                        cut = len(buf) - (2 << 20)
                        del buf[:cut]
                        self.term_base[tid] = self.term_base.get(tid, 0) + cut
                    live = ("term", tid, new)
            self._cond.notify_all()
        if live and live[0] == "ev" and self.on_event:
            self.on_event(live[1])
        elif live and live[0] == "term" and self.on_term:
            self.on_term(live[1], live[2])

    def request(self, op, timeout=60, **params):
        with self._cond:
            req_id = self._next_id
            self._next_id += 1
            self._ops[req_id] = op
        msg = dict(params, op=op, id=req_id)
        self.sock.sendall(encode_json(msg))
        deadline = time.monotonic() + timeout
        with self._cond:
            while req_id not in self._responses:
                left = deadline - time.monotonic()
                if left <= 0 or self._closed:
                    raise TimeoutError("no response to %s" % op)
                self._cond.wait(left)
            return self._responses.pop(req_id)

    def call(self, op, timeout=60, **params):
        resp = self.request(op, timeout=timeout, **params)
        if not resp.get("ok"):
            raise RuntimeError("%s failed: %s" % (op, resp.get("error")))
        return resp.get("data")

    def notify(self, op, **params):
        self.sock.sendall(encode_json(dict(params, op=op)))

    def term_write(self, term_id, data: bytes):
        self.sock.sendall(encode_term(term_id, data))

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        with self._cond:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def wait_state(self, topic, predicate, timeout=10):
        """Wait until a state event for `topic` satisfies predicate(data)."""
        return self.wait_for(lambda: self.state.get(topic) is not None and predicate(self.state[topic]),
                             timeout)

    def close(self):
        # shutdown() first: close() alone does not send FIN while the reader
        # thread is still blocked in recv() on the same socket.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
