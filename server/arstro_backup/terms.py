"""Shells on PTYs for the backup terminal (ADM-09).

A shell lives on while nobody watches it; a viewer that comes back gets its recent output again
(the last RING bytes), then the live output. Several viewers can watch one shell.
"""

import fcntl
import logging
import os
import pty
import pwd
import signal
import struct
import termios
import threading
import time

log = logging.getLogger("arstro.backup.terms")

RING = 256 * 1024                 # replayed to a viewer that attaches
MAX_TERMS = 16


def _winsize(fd, cols, rows):
    cols = max(2, min(int(cols), 1000))
    rows = max(2, min(int(rows), 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _shell():
    sh = os.environ.get("SHELL") or ""
    if not os.access(sh, os.X_OK):
        try:
            sh = pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            sh = ""
    return sh if os.access(sh or "/", os.X_OK) and sh else "/bin/sh"


class Term:
    def __init__(self, tid, cols, rows, on_exit):
        self.id = tid
        self.shell = _shell()
        self.created = time.time()
        self.lock = threading.Lock()
        self.ring = bytearray()
        self.clients = []                       # callables(bytes | dict) -> False to be dropped; never block
        self.exit_code = None
        self._on_exit = on_exit
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ARSTRO_", "NOTIFY_SOCKET", "INVOCATION_ID"))}
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
        pid, fd = pty.fork()
        if pid == 0:                            # the shell: a login shell in the home folder
            try:
                os.chdir(os.path.expanduser("~"))
            except OSError:
                pass
            try:
                os.execve(self.shell, ["-" + os.path.basename(self.shell)], env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        _winsize(fd, cols, rows)
        threading.Thread(target=self._reader, name="backup-term-%d" % tid, daemon=True).start()

    def _reader(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.ring += data
                if len(self.ring) > RING:
                    del self.ring[:len(self.ring) - RING]
                clients = list(self.clients)
            for c in clients:
                if c(data) is False:
                    self.detach(c)
        try:
            _, status = os.waitpid(self.pid, 0)
            self.exit_code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            self.exit_code = -1
        try:
            os.close(self.fd)
        except OSError:
            pass
        with self.lock:
            clients, self.clients = list(self.clients), []
        for c in clients:
            c({"t": "exit", "code": self.exit_code})
        log.info("shell %d ended (code %s)", self.id, self.exit_code)
        self._on_exit(self)

    def attach(self, client):
        """`client` gets the recent output, then the live output. It is called with this lock held
        and from the reader thread, so it must only queue, never block."""
        with self.lock:
            self.clients.append(client)
            if self.ring:
                client(bytes(self.ring))

    def detach(self, client):
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)

    def write(self, data):
        while data:
            try:
                n = os.write(self.fd, data)
            except OSError:
                return
            data = data[n:]

    def resize(self, cols, rows):
        try:
            _winsize(self.fd, cols, rows)           # the kernel sends SIGWINCH to the shell
        except OSError:
            pass

    def kill(self):
        for sig, wait in ((signal.SIGHUP, 2.0), (signal.SIGKILL, 0)):
            try:
                os.killpg(self.pid, sig)
            except OSError:
                return
            end = time.monotonic() + wait
            while self.exit_code is None and time.monotonic() < end:
                time.sleep(0.05)
            if self.exit_code is not None:
                return

    def describe(self):
        with self.lock:
            viewers = len(self.clients)
        return {"id": self.id, "shell": os.path.basename(self.shell), "created": int(self.created),
                "viewers": viewers}


class Terms:
    def __init__(self):
        self.lock = threading.Lock()
        self.terms = {}
        self.next_id = 1

    def open(self, cols=80, rows=24):
        with self.lock:
            if len(self.terms) >= MAX_TERMS:
                raise ValueError("%d shells are open already - close one first" % MAX_TERMS)
            tid, self.next_id = self.next_id, self.next_id + 1
        t = Term(tid, cols, rows, self._ended)
        with self.lock:
            self.terms[tid] = t
        log.info("shell %d opened (%s)", tid, t.shell)
        return t

    def _ended(self, t):
        with self.lock:
            self.terms.pop(t.id, None)

    def get(self, tid):
        with self.lock:
            return self.terms.get(tid)

    def list(self):
        with self.lock:
            terms = list(self.terms.values())
        return [t.describe() for t in sorted(terms, key=lambda t: t.id)]

    def close(self, tid):
        t = self.get(tid)
        if t is None:
            raise KeyError(tid)
        t.kill()

    def close_all(self):
        with self.lock:
            terms = list(self.terms.values())
        for t in terms:
            t.kill()
