"""Shared PTY shells (TERM-01..04).

Shells live in one daemon-wide pool and belong to nobody: every controller (app, web
page, CLI) sees the same list and can attach to any shell; several viewers mirror one
shell live and any of them can type. A shell nobody views is kept `term_keep_sec`
seconds (so a dropped Bluetooth link does not kill it) and re-attaching replays exactly
the output that was missed (TERM_OUT frames carry stream offsets). Shells opened with
`ephemeral=True` die with the session that opened them (scripts, one-shot CLI use).
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
    def __init__(self, term_id, cols, rows, pool, opened_by, ephemeral_owner=None):
        self.id = term_id
        self.pool = pool
        self.opened_by = opened_by            # controller kind that opened it
        self.ephemeral_owner = ephemeral_owner
        self.viewers = set()
        self.detached_at = None
        self.created = time.time()
        self.ring = bytearray()
        self.total_out = 0  # bytes ever produced; ring holds the tail ending here
        self.lock = threading.RLock()  # orders live output vs. replay on attach
        self.closed = False
        self._spawn(cols, rows)
        threading.Thread(target=self._reader, name="term-%d" % term_id, daemon=True).start()

    def _spawn(self, cols, rows):
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
        log.info("terminal %d opened pid=%d %sx%s by %s%s", self.id, pid, cols, rows, self.opened_by,
                 " (ephemeral)" if self.ephemeral_owner else "")

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
        return {"term": self.id, "kind": "shell", "cols": self.cols, "rows": self.rows,
                "viewers": len(self.viewers), "attached": bool(self.viewers),
                "opened_by": self.opened_by, "ephemeral": self.ephemeral_owner is not None,
                "age": int(time.time() - self.created),
                "detached_for": int(time.time() - self.detached_at) if self.detached_at else 0}


class TerminalPool:
    MAX_TERMINALS = 16

    def __init__(self, keep_sec=600, on_change=None):
        self.keep_sec = keep_sec
        self.on_change = on_change or (lambda terms: None)
        self._terms = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reaper, name="term-reaper", daemon=True).start()

    def _changed(self):
        try:
            self.on_change(self.list())
        except Exception:
            log.exception("terminals on_change failed")

    def _get(self, term_id):
        term = self._terms.get(int(term_id))
        if term is None or term.closed:
            raise KeyError("no terminal %s" % term_id)
        return term

    def list(self):
        with self._lock:
            return [t.describe() for t in sorted(self._terms.values(), key=lambda t: t.id)
                    if not t.closed]

    # ----------------------------------------------------------- lifecycle
    def open(self, session, cols=80, rows=24, ephemeral=False, factory=None):
        """Open a shell - or, with `factory(term_id, cols, rows, pool, opened_by, owner)`,
        another kind of terminal (a serial console, IO-09)."""
        factory = factory or Terminal
        with self._lock:
            if len(self._terms) >= self.MAX_TERMINALS:
                raise RuntimeError("too many shells (max %d)" % self.MAX_TERMINALS)
            term_id = next(i for i in range(256) if i not in self._terms)
            term = factory(term_id, cols, rows, self, session.controller,
                           ephemeral_owner=session if ephemeral else None)
            term.viewers.add(session)
            self._terms[term.id] = term
        self._changed()
        return term.id

    def attach(self, session, term_id, cols=None, rows=None, since=None, respond=None):
        """Add `session` as a viewer and replay the output it missed.

        `since` is the number of bytes the controller already has. If the ring still
        holds everything after that offset the replay is exact; otherwise the whole
        ring is replayed and the response says gap=true (controller resets its screen).
        `respond(data)` sends the JSON reply *before* the replay so the controller knows
        how to treat the bytes that follow."""
        term = self._get(term_id)
        with term.lock:
            ring_start = term.total_out - len(term.ring)
            if since is not None and ring_start <= int(since) <= term.total_out:
                start, gap = int(since), False
            else:
                start, gap = ring_start, True
            replay = bytes(term.ring[start - ring_start:])
            result = {"term": term.id, "replayed": len(replay), "start": start, "gap": gap,
                      "cols": term.cols, "rows": term.rows}
            if respond:
                respond(result)
            term.viewers.add(session)
            term.detached_at = None
            if replay:
                session.send_term(term.id, replay, start)
        if cols and rows:
            term.resize(cols, rows)
        log.info("terminal %d attached by session %d (%s), replayed %d bytes from %d, gap=%s",
                 term.id, session.num, session.controller, len(replay), start, gap)
        self._changed()
        return result

    def detach(self, session, term_id):
        term = self._get(term_id)
        with term.lock:
            term.viewers.discard(session)
            if not term.viewers:
                term.detached_at = time.time()
        self._changed()

    def detach_session(self, session):
        """A connection ended: drop it as a viewer; kill the shells it opened as ephemeral."""
        with self._lock:
            terms = list(self._terms.values())
        changed = False
        for t in terms:
            if t.ephemeral_owner is session:
                self.close_term(t, notify=False)
                changed = True
                continue
            with t.lock:
                if session in t.viewers:
                    t.viewers.discard(session)
                    changed = True
                    if not t.viewers:
                        t.detached_at = time.time()
                        log.info("terminal %d has no viewers (kept %ds)", t.id, self.keep_sec)
        if changed:
            self._changed()

    def write(self, session, term_id, data):
        term = self._terms.get(term_id)
        if term is not None and session in term.viewers:
            term.write(data)

    def resize(self, session, term_id, cols, rows):
        term = self._get(term_id)
        term.resize(cols, rows)   # last viewer to resize wins, like tmux "latest"
        self._changed()

    def close(self, _session, term_id):
        self.close_term(self._get(term_id))

    def close_term(self, term, notify=True):
        with self._lock:
            self._terms.pop(term.id, None)
        viewers = list(term.viewers)
        term.kill()
        for v in viewers:
            v.event("term.exit", term=term.id, code=None, reason="closed")
        if notify:
            self._changed()

    def close_all(self):
        with self._lock:
            terms = list(self._terms.values())
        for t in terms:
            self.close_term(t, notify=False)

    # ------------------------------------------------------------ callbacks
    def _output(self, term, data):
        with term.lock:
            offset = term.total_out
            term.append_ring(data)
            for v in list(term.viewers):
                try:
                    v.send_term(term.id, data, offset)
                except Exception:
                    term.viewers.discard(v)
                    if not term.viewers:
                        term.detached_at = time.time()

    def _exited(self, term, code):
        with self._lock:
            self._terms.pop(term.id, None)
        for v in list(term.viewers):
            v.event("term.exit", term=term.id, code=code)
        self._changed()

    def _reaper(self):
        while True:
            time.sleep(15)
            now = time.time()
            with self._lock:
                expired = [t for t in self._terms.values()
                           if not t.viewers and t.detached_at and now - t.detached_at > self.keep_sec]
            for t in expired:
                log.info("terminal %d expired after %ds without viewers", t.id, self.keep_sec)
                self.close_term(t)
