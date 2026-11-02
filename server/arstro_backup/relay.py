"""The backup terminal's own way in from the internet (ADM-10): a second device at the device manager.

The agent is the frozen copy of Arstro Remote's (vendor/agent.py, copied at install time), with
an identity of its own (relay_identity.json: id `<host>-backup-xxxxxx`, secret, access key), so the
backup stays reachable when the main server is stopped, broken or being updated. Viewers reach
only the backup's own port.

    relay.json   {"url", "token", "name", "enabled"} - the installer copies url and token from slot A
"""

import importlib.util
import json
import logging
import os
import platform
import re
import secrets
import socket
import string

from . import VERSION
from .vendor import e2e

log = logging.getLogger("arstro.backup.relay")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:24].strip("-")


def default_name():
    return ("%s (backup terminal)" % (socket.gethostname() or "arstro"))[:64]


def _write_private(path, obj):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def load_identity(cfg_dir, create=True):
    """{id, secret, key} of the backup (made, 0600, when there is none)."""
    path = os.path.join(cfg_dir, "relay_identity.json")
    try:
        with open(path) as f:
            d = json.load(f)
        if ID_RE.match(str(d.get("id"))) and d.get("secret") and e2e.parse_key(d.get("key")):
            return d
        log.error("%s is damaged - making a new identity", path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.error("%s unreadable (%s) - making a new identity", path, e)
    if not create:
        return None
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    ident = "%s-backup-%s" % (_slug(socket.gethostname()) or "arstro", suffix)
    if not ID_RE.match(ident):
        ident = "arstro-backup-" + suffix
    d = {"id": ident, "secret": secrets.token_urlsafe(32), "key": e2e.new_key()}
    os.makedirs(cfg_dir, exist_ok=True)
    _write_private(path, d)
    log.info("new device identity %s", ident)
    return d


def load_settings(cfg_dir):
    try:
        with open(os.path.join(cfg_dir, "relay.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {"url": d.get("url") or None, "token": d.get("token") or None, "name": d.get("name") or None,
            "enabled": bool(d.get("enabled"))}


def save_settings(cfg_dir, settings):
    os.makedirs(cfg_dir, exist_ok=True)
    _write_private(os.path.join(cfg_dir, "relay.json"), settings)


def crypto_missing():
    if importlib.util.find_spec("cryptography") is None:
        return "python3-cryptography is not installed"
    return None


class Relay:
    def __init__(self, cfg_dir, port, on_change=None):
        self.cfg_dir = cfg_dir
        self.port = port
        self.on_change = on_change
        self.agent = None
        self.identity = None
        self.settings = None

    def start(self):
        self.settings = load_settings(self.cfg_dir)
        self.identity = load_identity(self.cfg_dir)
        s, ident = self.settings, self.identity
        if not (s["enabled"] and s["url"] and s["token"]):
            log.info("device manager: off (no URL / join token set)")
            return
        from .vendor.agent import Agent
        self.agent = Agent(on_change=self._changed)
        self.agent.configure({
            "url": s["url"], "token": s["token"], "name": s["name"] or default_name(), "enabled": True,
            "ports": [self.port], "web": self.port, "id": ident["id"], "secret": ident["secret"],
            "key_raw": e2e.parse_key(ident["key"]), "version": "backup-" + VERSION,
            "hostname": socket.gethostname(), "platform": ("%s %s" % (platform.system(), platform.machine()))[:64],
            "slot": "backup", "blocked": crypto_missing()})
        self.agent.start()
        log.info("device manager %s: device %s, port %d", s["url"], ident["id"], self.port)

    def _changed(self):
        if self.on_change:
            self.on_change(self.status())

    def status(self):
        s, ident, a = self.settings or {}, self.identity or {}, self.agent
        out = {"enabled": bool(s.get("enabled") and s.get("url") and s.get("token")), "url": s.get("url"),
               "name": s.get("name") or default_name(), "id": ident.get("id"),
               "key_id": e2e.key_id(e2e.parse_key(ident["key"])) if ident.get("key") else None}
        if a is not None:
            out.update(state=a.state, error=a.error, transport=a.transport, viewers=len(a.viewers()))
        else:
            out.update(state="off")
        return out

    def stop(self):
        if self.agent:
            self.agent.stop()
