"""PTY-backed shell sessions streamed over the connection.

Terminals live in a daemon-wide pool so they survive a dropped Bluetooth link:
when a client that identified itself (hello.client_id) disconnects, its shells are
detached and kept for `term_keep_sec`; the recent output is buffered and replayed
when the client reconnects and calls term.attach. Clients without a client_id
(CLI, tests) get their shells killed on disconnect.
"""

import errno
import fcntl
import logging
import os
import pwd
import pty
import select
import signal
import struct
import termios
import threading
import time

log = logging.getLogger("arstro.term")

RING_MAX = 128 * 1024


def _set_winsize(fd, rows, cols):
    rows = max(1, min(int(rows), 1000))
    cols = max(1, min(int(cols), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return rows, cols


class Terminal:
    def __init__(self, term_id, client_id, cols, rows, pool):
        self.id = term_id
        self.client_id = client_id
        self.pool = pool
        self.owner = None
        self.detached_at = None
        self.created = time.time()
        self.ring = bytearray()
        self.total_out = 0  # bytes ever produced; ring holds the tail ending here
        self.lock = threading.RLock()  # orders live output vs. replay on attach
        self.closed = False
        user = pwd.getpwuid(os.getuid())
        shell = user.pw_shell or "/bin/bash"
        env = {
            "HOME": user.pw_dir,
            "USER": user.pw_name,
            "LOGNAME": user.pw_name,
            "SHELL": shell,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "ARSTRO_REMOTE": "1",
        }
        for key in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            if key in os.environ:
                env[key] = os.environ[key]

        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(user.pw_dir)
                os.execve(shell, ["-" + os.path.basename(shell)], env)
            finally:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        self.rows, self.cols = _set_winsize(fd, rows, cols)
        threading.Thread(target=self._reader, name="term-%d" % term_id, daemon=True).start()
        log.info("terminal %d opened pid=%d %sx%s client=%s", term_id, pid, cols, rows, client_id)

    def _reader(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if not r:
                    if self.closed:
                        break
                    continue
                data = os.read(self.fd, 65536)
            except OSError as e:
                if e.errno not in (errno.EIO, errno.EBADF):
                    log.warning("terminal %d read error: %s", self.id, e)
                break
            if not data:
                break
            self.pool._output(self, data)
        code = self._reap()
        try:
            os.close(self.fd)
        except OSError:
            pass
        log.info("terminal %d exited code=%s", self.id, code)
        if not self.closed:
            self.closed = True
            self.pool._exited(self, code)

    def _reap(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if pid:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                if os.WIFSIGNALED(status):
                    return -os.WTERMSIG(status)
                return None
            if time.monotonic() > deadline:
                return None
            time.sleep(0.05)

    def append_ring(self, data):
        self.total_out += len(data)
        self.ring += data
        if len(self.ring) > RING_MAX:
            cut = len(self.ring) - RING_MAX
            nl = self.ring.find(b"\n", cut)  # restart replay on a line boundary
            del self.ring[:nl + 1 if 0 <= nl < cut + 4096 else cut]

    def write(self, data: bytes):
        if self.closed:
            return
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                select.select([], [self.fd], [], 1.0)
                continue
            except OSError:
                return
            view = view[n:]

    def resize(self, cols, rows):
        if not self.closed:
            try:
                # The kernel delivers SIGWINCH to the foreground process group.
                self.rows, self.cols = _set_winsize(self.fd, rows, cols)
            except OSError:
                pass

    def kill(self):
        if self.closed:
            return
        self.closed = True
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.pid), sig)
            except OSError:
                break
            if self._reap() is not None:
                break
        log.info("terminal %d killed", self.id)

    def describe(self):
        return {"term": self.id, "cols": self.cols, "rows": self.rows,
                "attached": self.owner is not None, "age": int(time.time() - self.created),
                "detached_for": int(time.time() - self.detached_at) if self.detached_at else 0}


class TerminalPool:
    MAX_PER_CLIENT = 8

    def __init__(self, keep_sec=600):
        self.keep_sec = keep_sec
        self._terms = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reaper, name="term-reaper", daemon=True).start()

    # ------------------------------------------------------------- lookups
    def _owned(self, session, term_id):
        term = self._terms.get(term_id)
        if term is None or term.owner is not session:
            raise KeyError("no terminal %s" % term_id)
        return term

    def list(self, session):
        with self._lock:
            return [t.describe() for t in self._terms.values()
                    if t.client_id == session.client_id and not t.closed]

    # ----------------------------------------------------------- lifecycle
    def open(self, session, cols=80, rows=24):
        with self._lock:
            mine = [t for t in self._terms.values() if t.client_id == session.client_id]
            if len(mine) >= self.MAX_PER_CLIENT:
                raise RuntimeError("too many terminals (max %d)" % self.MAX_PER_CLIENT)
            free = [i for i in range(256) if i not in self._terms]
            if not free:
                raise RuntimeError("terminal limit reached")
            term = Terminal(free[0], session.client_id, cols, rows, self)
            term.owner = session
            self._terms[term.id] = term
        return term.id

    def attach(self, session, term_id, cols=None, rows=None, since=None, respond=None):
        """Re-bind a terminal to `session` and replay the output it missed.

        `since` is the number of bytes the client already has. If the ring still
        holds everything after that offset the replay is exact; otherwise the
        whole ring is replayed and the response says gap=true (client resets its
        screen first). `respond(data)` sends the JSON reply *before* the replay so
        the client knows how to treat the bytes that follow."""
        with self._lock:
            term = self._terms.get(term_id)
            if term is None or term.closed or term.client_id != session.client_id:
                raise KeyError("no terminal %s" % term_id)
        with term.lock:
            ring_start = term.total_out - len(term.ring)
            if since is not None and ring_start <= int(since) <= term.total_out:
                start, gap = int(since), False
            else:
                start, gap = ring_start, True
            replay = bytes(term.ring[start - ring_start:])
            result = {"term": term_id, "replayed": len(replay), "start": start, "gap": gap,
                      "cols": term.cols, "rows": term.rows}
            if respond:
                respond(result)
            term.owner = session
            term.detached_at = None
            if replay:
                session.send_term(term.id, replay, start)
        if cols and rows:
            term.resize(cols, rows)
        log.info("terminal %d attached to session %d (replayed %d bytes from %d, gap=%s)",
                 term_id, session.num, len(replay), start, gap)
        return result

    def detach_session(self, session):
        """Called when a connection ends: keep (or kill) its terminals."""
        with self._lock:
            owned = [t for t in self._terms.values() if t.owner is session]
        for t in owned:
            with t.lock:
                t.owner = None
                t.detached_at = time.time()
            if not session.persistent:
                self.close_term(t)
            else:
                log.info("terminal %d detached at offset %d (kept %ds)", t.id, t.total_out, self.keep_sec)

    def write(self, session, term_id, data):
        term = self._terms.get(term_id)
        if term is not None and term.owner is session:
            term.write(data)

    def resize(self, session, term_id, cols, rows):
        self._owned(session, term_id).resize(cols, rows)

    def close(self, session, term_id):
        with self._lock:
            term = self._terms.get(term_id)
            if term is None or term.client_id != session.client_id:
                raise KeyError("no terminal %s" % term_id)
        self.close_term(term)

    def close_term(self, term):
        with self._lock:
            self._terms.pop(term.id, None)
        term.kill()

    def close_all(self):
        with self._lock:
            terms = list(self._terms.values())
        for t in terms:
            self.close_term(t)

    # ------------------------------------------------------------ callbacks
    def _output(self, term, data):
        with term.lock:
            offset = term.total_out
            term.append_ring(data)
            owner = term.owner
            if owner is None:
                return
            try:
                owner.send_term(term.id, data, offset)
            except Exception:
                term.owner = None
                term.detached_at = time.time()

    def _exited(self, term, code):
        with self._lock:
            self._terms.pop(term.id, None)
        owner = term.owner
        if owner is not None:
            owner.event("term.exit", term=term.id, code=code)

    def _reaper(self):
        while True:
            time.sleep(15)
            now = time.time()
            with self._lock:
                expired = [t for t in self._terms.values()
                           if t.owner is None and t.detached_at and now - t.detached_at > self.keep_sec]
            for t in expired:
                log.info("terminal %d expired after %ds detached", t.id, self.keep_sec)
                self.close_term(t)
