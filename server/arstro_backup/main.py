"""The backup terminal's command line (ADM-09, ADM-10).

    arstro-backup run                 serve (what the arstro-backup service runs)
    arstro-backup status              port, whether it runs, the device manager connection
    arstro-backup key                 the access key: to open the backup through the device manager
    arstro-backup password [--set PW] where the password is / change it (applies at once)
    arstro-backup relay [--url U --token T] [--name N] [--on | --off]
                                      the device manager connection (restart the service to apply)

Files: $XDG_CONFIG_HOME/arstro-backup/ (config.json {port, bind}, password, relay.json,
relay_identity.json), $XDG_STATE_HOME/arstro-backup/backup.log, and while it runs
$XDG_RUNTIME_DIR/arstro-backup.json (pid, port, the device manager state).
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading

from . import VERSION

DEFAULT_PORT = 8181


def config_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "arstro-backup")


def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "arstro-backup")


def runtime_file():
    run = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(run, "arstro-backup.json")


def load_config():
    try:
        with open(os.path.join(config_dir(), "config.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    port = d.get("port") if isinstance(d.get("port"), int) and 0 < d.get("port") < 65536 else DEFAULT_PORT
    return {"port": port, "bind": d.get("bind") if isinstance(d.get("bind"), str) else "0.0.0.0"}


def password_path():
    return os.path.join(config_dir(), "password")


def _logging():
    os.makedirs(state_dir(), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(state_dir(), "backup.log"), maxBytes=1 << 20, backupCount=2)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    if sys.stderr.isatty():
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)


def _write_runtime(d):
    path = runtime_file()
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError:
        pass


def cmd_run(a):
    _logging()
    log = logging.getLogger("arstro.backup")
    from .relay import Relay
    from .terms import Terms
    from .web import Password, serve
    cfg = load_config()
    port = a.port or cfg["port"]
    pw = Password(password_path())
    if not pw.get():
        log.error("no password in %s - run the installer (install.sh --backup) or arstro-backup password --set",
                  password_path())
        return 2
    terms = Terms()
    srv = serve(cfg["bind"], port, terms, pw, socket.gethostname())
    state = {"pid": os.getpid(), "port": port, "version": VERSION, "relay": {"state": "off"}}
    lock = threading.Lock()

    def relay_changed(st):
        with lock:
            if st != state["relay"]:
                if st.get("state") != state["relay"].get("state"):
                    log.info("device manager: %s%s", st.get("state"), " - %s" % st["error"] if st.get("error") else "")
                state["relay"] = st
                _write_runtime(state)

    relay = Relay(config_dir(), port, relay_changed)
    try:
        relay.start()
    except Exception:                                   # the shells work without the device manager
        log.exception("the device manager connection did not start")
    state["relay"] = relay.status()
    _write_runtime(state)

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("arstro backup terminal %s on port %d (pid %d)", VERSION, port, os.getpid())
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        relay.stop()
        terms.close_all()
        srv.server_close()
        try:
            os.remove(runtime_file())
        except OSError:
            pass
        log.info("stopped")
    return 0


def cmd_status(a):
    cfg = load_config()
    try:
        with open(runtime_file()) as f:
            st = json.load(f)
        os.kill(st["pid"], 0)
    except (OSError, ValueError, KeyError):
        st = None
    from .relay import load_identity, load_settings
    ident = load_identity(config_dir(), create=False) or {}
    rs = load_settings(config_dir())
    out = {"version": VERSION, "port": cfg["port"], "running": bool(st), "pid": st and st.get("pid"),
           "password_set": os.path.exists(password_path()), "device_id": ident.get("id"),
           "device_manager": (st or {}).get("relay") or {"url": rs["url"], "enabled": rs["enabled"], "state": "not running"}}
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    print("Backup terminal %s - %s, port %d" % (VERSION, "running (pid %s)" % out["pid"] if st else "NOT running", cfg["port"]))
    for ip in _addresses():
        print("   http://%s:%d/" % (ip, cfg["port"]))
    dm = out["device_manager"]
    print("Device manager: %s%s%s" % (dm.get("state"), " - %s" % dm["error"] if dm.get("error") else "",
                                     " (%s)" % (dm.get("url") or "no URL set")))
    print("Device id: %s   (its access key: arstro-backup key)" % (ident.get("id") or "-"))
    return 0 if st else 1


def _addresses():
    out = []
    try:                                   # the address of the default route (a UDP "connect" sends nothing)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            out.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out or ["127.0.0.1"]


def cmd_key(_a):
    from .relay import load_identity
    ident = load_identity(config_dir())
    print(ident["key"])
    return 0


def cmd_password(a):
    path = password_path()
    if a.set is None:
        print("The password is in %s (the installer copied slot A's; change it with --set)." % path)
        return 0
    pw = a.set.strip()
    if len(pw) < 4 or any(c.isspace() for c in pw):
        print("error: the password needs 4 or more characters and no spaces", file=sys.stderr)
        return 2
    os.makedirs(config_dir(), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw + "\n")
    os.replace(path + ".tmp", path)
    print("Password changed (logged-in browsers have to log in again).")
    return 0


def cmd_relay(a):
    from .relay import load_settings, save_settings
    s = load_settings(config_dir())
    if a.url is not None:
        s["url"] = a.url.strip() or None
    if a.token is not None:
        s["token"] = a.token.strip() or None
    if a.name is not None:
        s["name"] = a.name.strip() or None
    if a.on:
        s["enabled"] = True
    if a.off:
        s["enabled"] = False
    changed = any(v is not None for v in (a.url, a.token, a.name)) or a.on or a.off
    if changed:
        if s["enabled"] and not (s["url"] and s["token"]):
            print("error: the device manager needs --url and --token", file=sys.stderr)
            return 2
        save_settings(config_dir(), s)
        print("Saved - apply it with: systemctl --user restart arstro-backup")
    print("Device manager: %s, %s, join token %s, name %s" % (
        "on" if s["enabled"] else "off", s["url"] or "no URL", "set" if s["token"] else "not set", s["name"] or "(default)"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arstro-backup", description="Arstro backup terminal %s" % VERSION)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="serve (what the service runs)")
    r.add_argument("--port", type=int, help="instead of config.json's (default %d)" % DEFAULT_PORT)
    st = sub.add_parser("status", help="port, whether it runs, the device manager connection")
    st.add_argument("--json", action="store_true")
    sub.add_parser("key", help="the access key (to open the backup through the device manager)")
    pw = sub.add_parser("password", help="where the password is, or --set a new one")
    pw.add_argument("--set", metavar="PW")
    rl = sub.add_parser("relay", help="the device manager connection")
    rl.add_argument("--url")
    rl.add_argument("--token")
    rl.add_argument("--name")
    g = rl.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    a = ap.parse_args(argv)
    fn = {"run": cmd_run, "status": cmd_status, "key": cmd_key, "password": cmd_password, "relay": cmd_relay}.get(a.cmd)
    if fn is None:
        ap.print_help()
        return 2
    if a.cmd == "status" and not hasattr(a, "json"):
        a.json = False
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
