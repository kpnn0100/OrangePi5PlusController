"""Which A/B slot hosts the running session, and which one is idle (ADM-08).

A shell opened in a web terminal (or the app's terminal) is a child of that slot's daemon,
and so is everything started from it - an editor, an agent, `install.sh`. Reinstalling or
restarting that slot kills all of them. `arstro-remote slots` answers the question before
a deploy by walking the process tree up to a slot's daemon (whose pid its lock file holds):

    arstro-remote slots             table: slot, port, running, pid, hosts-this-session
    arstro-remote slots --current   the slot hosting this process ("-" if none)
    arstro-remote slots --idle      the slot to deploy into (never the current one)

No server is needed; it reads /proc, the lock files and the slots' config.json.
"""

import glob
import json
import os
import re

from .paths import instance_name, runtime_dir

LEGACY = "legacy"          # an old, unslotted install (arstro-remote.lock); it occupies slot A's port


def _pid_in(lock):
    try:
        with open(lock) as f:
            txt = f.read(20)
    except OSError:
        return None
    m = re.match(r"\s*(\d+)", txt)
    if not m:
        return None
    pid = int(m.group(1))
    return pid if os.path.exists("/proc/%d" % pid) else None


def _ancestors(pid):
    out = []
    seen = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        out.append(pid)
        try:
            with open("/proc/%d/stat" % pid) as f:
                stat = f.read()
            pid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def _port(cfg_dir, default):
    try:
        with open(os.path.join(cfg_dir, "config.json")) as f:
            return int(json.load(f).get("web_port", default))
    except (OSError, ValueError, TypeError):
        return default


def slots(pid=None):
    """[{slot, instance, installed, running, pid, port, current}] for every known slot plus a
    legacy install, sorted a, b, ..., legacy."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    names = set()
    for d in glob.glob(os.path.join(xdg_cfg, "arstro-remote-[a-z]")) + \
            glob.glob(os.path.join(xdg_data, "arstro-remote-[a-z]")) + \
            glob.glob(os.path.join(runtime_dir(), "arstro-remote-[a-z].lock")):
        m = re.search(r"arstro-remote-([a-z])(?:\.lock)?$", d)
        if m:
            names.add(m.group(1))
    chain = set(_ancestors(pid or os.getpid()))
    out = []
    for s in sorted(names) + [LEGACY]:
        inst = instance_name(None if s == LEGACY else s) if s != LEGACY else "arstro-remote"
        daemon = _pid_in(os.path.join(runtime_dir(), inst + ".lock"))
        launcher = _pid_in(os.path.join(runtime_dir(), inst + "-launcher.lock"))
        installed = os.path.isdir(os.path.join(xdg_data, inst, "arstro_remote")) or \
            os.path.exists(os.path.join(xdg_cfg, "autostart", inst + ".desktop"))
        if s == LEGACY and not installed and not daemon:
            continue
        default_port = 8080 + (ord(s) - ord("a") if s != LEGACY else 0)
        out.append({"slot": s, "instance": inst, "installed": installed, "running": bool(daemon),
                    "pid": daemon, "port": _port(os.path.join(xdg_cfg, inst), default_port),
                    "current": bool(daemon and daemon in chain) or bool(launcher and launcher in chain)})
    return out


def current(pid=None):
    """The slot hosting `pid` (default: this process), or None."""
    for s in slots(pid):
        if s["current"]:
            return s["slot"]
    return None


def idle(pid=None):
    """The slot to deploy into: never the one hosting this session. A legacy install counts as
    slot A (it owns A's port), so while a session runs in the legacy instance the idle slot is B,
    and while it runs in B the idle slot is A (installing A replaces the legacy one)."""
    cur = current(pid)
    host = "a" if cur == LEGACY else cur
    for cand in ("a", "b"):
        if cand != host:
            return cand
    return None
