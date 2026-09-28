"""Persistent logging: everything the app (and GStreamer, from C) prints goes to
~/.local/state/hdmi-recorder/recorder.log with timestamps, and still to the terminal.

The previous run is kept as recorder.log.1, so a crash can be looked at after a restart.
"""
import atexit
import os
import sys
import threading
import time

LOG_DIR = os.path.expanduser("~/.local/state/hdmi-recorder")
LOG_PATH = os.path.join(LOG_DIR, "recorder.log")
TERMINAL_FD = None      # the original stdout, for callers that draw on the terminal


def _rotate():
    try:
        if os.path.exists(LOG_PATH):
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def setup(terminal=True):
    """Tee fd 1 and 2 (Python and C output) into the log file. Returns the log path.
    terminal=False: log only; the caller writes to TERMINAL_FD itself (CLI status line)."""
    global TERMINAL_FD
    os.makedirs(LOG_DIR, exist_ok=True)
    _rotate()
    logf = open(LOG_PATH, "a", buffering=1, encoding="utf-8", errors="replace")
    lock = threading.Lock()
    pumps = []
    sys.stdout.flush()
    sys.stderr.flush()
    for fd, tag in ((1, ""), (2, "")):
        term = os.dup(fd)
        if fd == 1:
            TERMINAL_FD = term
        r, w = os.pipe()
        os.dup2(w, fd)
        os.close(w)

        def pump(r=r, term=term, tag=tag):
            with os.fdopen(r, "rb", buffering=0) as src:
                buf = b""
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    if terminal:
                        try:
                            os.write(term, chunk)
                        except OSError:
                            pass
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    if lines:
                        stamp = time.strftime("%H:%M:%S")
                        with lock:
                            for line in lines:
                                text = line.decode("utf-8", "replace").rstrip("\r")
                                if text.strip():
                                    logf.write(f"{stamp} {tag}{text}\n")
        t = threading.Thread(target=pump, daemon=True, name=f"log-fd{fd}")
        t.start()
        pumps.append(t)
    sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8", errors="replace")
    sys.stderr = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")

    def drain():
        # close every write end of the pipes so the pumps see EOF and write the last lines
        for f in (sys.stdout, sys.stderr):
            try:
                f.flush()
                f.close()
            except (OSError, ValueError):
                pass
        for fd in (1, 2):
            try:
                os.close(fd)
            except OSError:
                pass
        for t in pumps:
            t.join(timeout=2)
    atexit.register(drain)
    print(f"[info] ===== hdmi-recorder started {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"(log: {LOG_PATH}) =====")
    return LOG_PATH


def tail(path=LOG_PATH, lines=200):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return f"(no log at {path})\n"
