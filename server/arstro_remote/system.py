"""System module: server/slot info, modules, logs (LOG-01..04), restart, reboot (ADM-04..07)."""

import glob
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time

from . import __version__, modules
from .paths import config_dir, control_socket_path, install_dir, instance_name, save_config_values, slot, state_dir
from .session import OpError

log = logging.getLogger("arstro.system")

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
TAIL_MAX = 2000


def log_files():
    """{name: path} of this instance's logs (current files, not the rotated .1 .2 ...)."""
    d = state_dir()
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "*.log"))):
        out[os.path.basename(p)[:-4]] = p
    return out


def tail(path, lines=200, grep=None, level=None):
    lines = max(1, min(int(lines), TAIL_MAX))
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    chunk = min(size, max(64 * 1024, lines * 400))
    with open(path, "rb") as f:
        f.seek(size - chunk)
        data = f.read().decode("utf-8", "replace").splitlines()
    if size > chunk and data:
        data = data[1:]                    # first line is probably cut
    if grep:
        g = grep.lower()
        data = [l for l in data if g in l.lower()]
    if level and level in LEVELS and level != "debug":
        keep = [n.upper() for n, v in LEVELS.items() if v >= LEVELS[level]]
        data = [l for l in data if any(" %s " % k in l[:40] or " %-7s " % k in l[:40] for k in keep)]
    return data[-lines:]


class SystemService:
    def __init__(self, ctx):
        self.ctx = ctx

    def start(self):
        pass

    def shutdown(self):
        pass

    def handle(self, session, op, msg):
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            raise OpError("unknown op %s" % op)
        return fn(session, msg)

    # ---------------------------------------------------------------- info
    def info(self):
        from . import boards
        board = boards.detect()
        return {
            "version": __version__, "slot": slot(), "instance": instance_name(),
            "port": self.ctx.config.get("web_port"), "pid": os.getpid(),
            "hostname": socket.gethostname(), "machine": platform.machine(),
            "kernel": platform.release(), "python": platform.python_version(),
            "board": board.NAME if board else None,
            "model": boards.dt.model(),
            "uptime": int(time.time() - self.ctx.started),
            "paths": {"code": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "install": install_dir(), "config": config_dir(), "logs": state_dir(),
                      "socket": control_socket_path()},
            "bluetooth": bool(self.ctx.use_bluetooth),
            "modules": modules.describe(self.ctx.modules, self.ctx.module_ok),
            "log_level": logging.getLevelName(logging.getLogger().level).lower(),
        }

    def op_system_info(self, _s, _m):
        return self.info()

    def op_system_modules(self, _s, _m):
        return {"modules": modules.describe(self.ctx.modules, self.ctx.module_ok)}

    def op_system_modules_set(self, session, msg):
        """Choose the enabled modules (saved; applied by a server restart)."""
        wanted = msg.get("modules")
        if not isinstance(wanted, list) or any(m not in modules.MODULES for m in wanted):
            raise OpError("modules must be a list of: %s" % ", ".join(modules.MODULES))
        wanted = [m for m in modules.MODULES if m in wanted or m in modules.ALWAYS]
        save_config_values({"modules": wanted})
        log.info("modules set to %s by session %d (%s)", ",".join(wanted), session.num, session.controller)
        restart = bool(msg.get("restart", True))
        if restart:
            self._restart_later("modules changed")
        return {"modules": wanted, "restarting": restart}

    def _restart_later(self, reason, delay=0.8):
        def go():
            time.sleep(delay)
            log.warning("restarting the server: %s", reason)
            self.ctx.request_stop(exit_code=0)
        threading.Thread(target=go, name="restart", daemon=True).start()

    def op_system_restart(self, session, _m):
        """Restart this server (the launcher starts it again within seconds)."""
        log.info("restart requested by session %d (%s)", session.num, session.controller)
        self._restart_later("requested by %s" % session.controller)
        return {"restarting": True}

    def _power(self, session, what):
        log.warning("%s requested by session %d (%s)", what, session.num, session.controller)
        try:
            r = subprocess.run(["systemctl", what], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OpError("%s failed: %s" % (what, e))
        if r.returncode != 0:
            raise OpError("%s refused: %s" % (what, (r.stderr or r.stdout).strip()[-300:]))
        return {"ok": True}

    def op_system_reboot(self, session, _m):
        return self._power(session, "reboot")

    def op_system_poweroff(self, session, _m):
        return self._power(session, "poweroff")

    # ----------------------------------------------------------------- logs
    def op_log_files(self, _s, _m):
        out = []
        for name, p in log_files().items():
            try:
                st = os.stat(p)
                out.append({"name": name, "path": p, "size": st.st_size, "mtime": int(st.st_mtime)})
            except OSError:
                pass
        return {"files": out, "dir": state_dir()}

    def op_log_tail(self, _s, msg):
        name = msg.get("file") or "arstro-remote"
        files = log_files()
        if name not in files:
            raise OpError("no log %r (have: %s)" % (name, ", ".join(files) or "none"))
        return {"file": name, "path": files[name],
                "lines": tail(files[name], msg.get("lines", 200), msg.get("grep"), msg.get("level"))}

    def op_log_level(self, session, msg):
        level = msg.get("level")
        if level:
            if level not in LEVELS:
                raise OpError("level must be one of %s" % ", ".join(LEVELS))
            logging.getLogger().setLevel(LEVELS[level])
            if msg.get("save"):
                save_config_values({"log_level": level})
            log.warning("log level set to %s by session %d (%s)", level, session.num, session.controller)
        return {"level": logging.getLevelName(logging.getLogger().level).lower()}

    def op_log_mark(self, session, msg):
        """Write a marker line into the log (\"now I press the button\")."""
        text = str(msg.get("text") or "mark")[:200]
        log.warning("MARK from %s: %s", session.controller, text)
        return {}


def install_exception_logging():
    """Uncaught exceptions in any thread land in the log with a traceback; a hard crash
    (segfault in a C library) leaves a Python stack in crash.log (LOG-03)."""
    def hook(exc_type, exc, tb):
        logging.getLogger("arstro").critical("uncaught exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = hook

    def thook(args):
        if args.exc_type is SystemExit:
            return
        logging.getLogger("arstro").error("uncaught exception in thread %s",
                                          args.thread.name if args.thread else "?",
                                          exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thook
    try:
        import faulthandler
        os.makedirs(state_dir(), exist_ok=True)
        f = open(os.path.join(state_dir(), "crash.log"), "a")
        f.write("=== %s start pid %d\n" % (time.strftime("%F %T"), os.getpid()))
        f.flush()
        faulthandler.enable(f)
        return f
    except OSError:
        return None
