"""Internet access (module `relay`, RLY-01..08): this slot advertises itself to a device
manager and serves end-to-end encrypted viewer channels to its local ports.

Files in the slot's config dir (both 0600):
    relay_identity.json   {id, secret, key}  made on the first start, even while switched off
    relay.json            {url, token, name, enabled, ports}  - defaults from config.json "relay"

Ops: relay.status, relay.set, relay.key, relay.rotate, relay.kick. Topic `relay` (never the
key, the join token or the secret). No outbound connection until a URL and a join token are
set and it is switched on.
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
import threading
import urllib.parse

from .. import __version__
from ..paths import config_dir, slot
from ..session import OpError
from . import e2e

log = logging.getLogger("arstro.relay")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
SETTINGS = ("url", "token", "name", "enabled", "ports")
TICK = 2.0                     # s between viewer byte-counter updates while someone is connected


def crypto_missing():
    """Why the module cannot work here, or None."""
    if importlib.util.find_spec("cryptography") is None:
        return "python3-cryptography is not installed"
    return None


# ------------------------------------------------------------------------ files
def identity_path(cfg_dir=None):
    return os.path.join(cfg_dir or config_dir(), "relay_identity.json")


def settings_path(cfg_dir=None):
    return os.path.join(cfg_dir or config_dir(), "relay.json")


def _write_private(path, data, exclusive=False):
    """Write JSON with mode 0600 atomically. exclusive=True: only if `path` does not exist yet
    (returns False when another process was first)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        if exclusive:
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False
            except OSError:                      # no hard links on this file system
                if os.path.exists(path):
                    return False
                os.replace(tmp, path)
                tmp = None
            return True
        os.replace(tmp, path)
        tmp = None
        return True
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:24].strip("-")


def new_identity():
    host = _slug(socket.gethostname()) or "arstro"
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    ident = "-".join(p for p in (host, slot() or "", suffix) if p)
    if not ID_RE.match(ident):
        ident = "arstro-" + suffix
    return {"id": ident, "secret": secrets.token_urlsafe(32), "key": e2e.new_key()}


def _valid_identity(d):
    try:
        return (isinstance(d, dict) and ID_RE.match(d["id"]) and isinstance(d["secret"], str)
                and len(d["secret"]) >= 32 and len(e2e.parse_key(d["key"])) == e2e.KEY_LEN)
    except (KeyError, TypeError, ValueError):
        return False


def load_identity(cfg_dir=None, create=True):
    """The slot's identity {id, secret, key}; made (0600) when there is none."""
    path = identity_path(cfg_dir)
    for _ in range(3):
        try:
            with open(path) as f:
                d = json.load(f)
            if _valid_identity(d):
                if os.stat(path).st_mode & 0o077:
                    os.chmod(path, 0o600)
                return d
            log.error("relay identity %s is damaged - making a new one (the old file is kept as .bad)", path)
            os.replace(path, path + ".bad")
        except FileNotFoundError:
            pass
        except ValueError:
            log.error("relay identity %s is not JSON - making a new one (the old file is kept as .bad)", path)
            os.replace(path, path + ".bad")
        if not create:
            return None
        d = new_identity()
        if _write_private(path, d, exclusive=True):
            log.info("internet access: new device identity %s (access key id %s)", d["id"],
                     e2e.key_id(e2e.parse_key(d["key"])))
            return d
    raise RuntimeError("cannot create %s" % path)


def default_name():
    host = socket.gethostname() or "arstro"
    s = slot()
    return ("%s (slot %s)" % (host, s.upper()) if s else host)[:64]


# ------------------------------------------------------------------------ validation
def check_url(url):
    if not isinstance(url, str) or not url.strip():
        raise OpError("the device manager URL is empty")
    url = url.strip()
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise OpError("the device manager URL must start with https:// (or http://) and name a host")
    if u.username or u.password:
        raise OpError("put no user name or password into the URL (the join token is set separately)")
    if u.query or u.fragment:
        raise OpError("the device manager URL takes no ?query or #fragment")
    try:
        u.port
    except ValueError:
        raise OpError("the port in the device manager URL is not a number")
    return url.rstrip("/")


def check_token(token):
    if not isinstance(token, str) or not token.strip():
        raise OpError("the join token is empty")
    token = token.strip()
    if len(token) > 512 or any(c.isspace() or not c.isprintable() for c in token):
        raise OpError("the join token has characters that do not belong in it")
    return token


def check_name(name):
    if not isinstance(name, str):
        raise OpError("the name must be text")
    name = name.strip()
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise OpError("the name must be 1-64 printable characters")
    return name


def check_ports(ports):
    """None (any port) or a sorted list of distinct ports 1-65535. Text '8080, 22' / 'any' too."""
    if ports is None:
        return None
    if isinstance(ports, str):
        t = ports.strip().lower()
        if t in ("", "any", "all", "*"):
            return None
        ports = [p for p in re.split(r"[\s,]+", t) if p]
    if not isinstance(ports, (list, tuple)):
        raise OpError("ports must be a list of port numbers or null (any port)")
    out = set()
    for p in ports:
        if isinstance(p, str) and p.strip().isdigit():
            p = int(p.strip())
        if isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535:
            raise OpError("%r is not a port number (1-65535)" % (p,))
        out.add(p)
    if not out:
        raise OpError("give at least one port, or null (any port)")
    return sorted(out)


def load_settings(config, cfg_dir=None):
    """relay.json, else config.json's "relay" (defaults), with bad values dropped."""
    data = None
    try:
        with open(settings_path(cfg_dir)) as f:
            data = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.error("ignoring bad %s: %s", settings_path(cfg_dir), e)
    if not isinstance(data, dict):
        data = config.get("relay") if isinstance(config.get("relay"), dict) else {}
    out = {"url": None, "token": None, "name": None, "enabled": False, "ports": None}
    for key, check in (("url", check_url), ("token", check_token), ("name", check_name), ("ports", check_ports)):
        if data.get(key) is not None:
            try:
                out[key] = check(data[key])
            except OpError as e:
                log.error("relay setting %s ignored: %s", key, e)
    out["enabled"] = bool(data.get("enabled", False))
    return out


def save_settings(settings, cfg_dir=None):
    _write_private(settings_path(cfg_dir), {k: settings.get(k) for k in SETTINGS})


def platform_text():
    text = "%s %s" % (platform.system(), platform.machine())
    try:
        from ..hwio import dt
        model = dt.model()
    except Exception:
        model = None
    return ("%s (%s)" % (text, model) if model else text)[:64]


# ------------------------------------------------------------------------ the service
class RelayService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.RLock()
        self.identity = None
        self.settings = None
        self.agent = None
        self.blocked = crypto_missing()
        self._stop = threading.Event()
        self._platform = None

    # -- lifecycle ------------------------------------------------------------------------
    def start(self):
        from .agent import Agent
        self.identity = load_identity()
        self.settings = load_settings(self.ctx.config)
        self._platform = platform_text()
        self.agent = Agent(on_change=self.publish)
        self.agent.configure(self._agent_cfg())
        self.agent.start()
        threading.Thread(target=self._ticker, name="relay-tick", daemon=True).start()
        s = self.settings
        log.info("internet access: device %s, %s%s", self.identity["id"],
                 "on, device manager %s" % s["url"] if s["enabled"] and s["url"] else
                 "on, but no device manager set" if s["enabled"] else "off",
                 " - %s" % self.blocked if self.blocked else "")
        self.publish()

    def shutdown(self):
        self._stop.set()
        if self.agent:
            self.agent.stop()

    def _ticker(self):
        while not self._stop.wait(TICK):
            if self.agent and self.agent.has_viewers():
                self.publish()                 # byte counters (the hub drops unchanged values)

    def _agent_cfg(self):
        s, ident = self.settings, self.identity
        return {"url": s["url"], "token": s["token"], "name": s["name"] or default_name(),
                "enabled": s["enabled"], "ports": s["ports"], "id": ident["id"], "secret": ident["secret"],
                "key_raw": e2e.parse_key(ident["key"]), "version": __version__, "hostname": socket.gethostname(),
                "platform": self._platform, "slot": slot(), "web": self.ctx.config.get("web_port"),
                "blocked": self.blocked}

    # -- state ----------------------------------------------------------------------------
    def status(self):
        with self.lock:
            s, ident, a = self.settings, self.identity, self.agent
            return {
                "enabled": s["enabled"], "url": s["url"], "name": s["name"] or default_name(),
                "id": ident["id"], "key_id": e2e.key_id(e2e.parse_key(ident["key"])),
                "state": a.state, "error": a.error, "since": int(a.since), "relay_version": a.relay_version,
                "transport": a.transport,
                "token_set": bool(s["token"]), "ports": s["ports"], "viewers": a.viewers(),
                "wrong_keys": a.wrong_total,
            }

    def publish(self):
        if self.agent is None or self.identity is None:
            return
        self.ctx.hub.publish("relay", self.status())

    # -- ops ------------------------------------------------------------------------------
    def handle(self, session, op, msg):
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            raise OpError("unknown op %s" % op)
        return fn(session, msg)

    def op_relay_status(self, _s, _m):
        return self.status()

    def op_relay_set(self, session, msg):
        new = {}
        if "url" in msg:
            new["url"] = None if msg["url"] in (None, "") else check_url(msg["url"])
        if "token" in msg:
            new["token"] = check_token(msg["token"])
        if "name" in msg:
            new["name"] = None if msg["name"] in (None, "") else check_name(msg["name"])
            if new["name"] == default_name():
                new["name"] = None                  # the default stays a default (follows the host name)
        if "enabled" in msg:
            if not isinstance(msg["enabled"], bool):
                raise OpError("enabled must be true or false")
            new["enabled"] = msg["enabled"]
        if "ports" in msg:
            new["ports"] = check_ports(msg["ports"])
        if not new:
            raise OpError("nothing to change: give url, token, name, enabled or ports")
        with self.lock:
            merged = dict(self.settings, **new)
            if merged["enabled"] and not (merged["url"] and merged["token"]):
                raise OpError("set the device manager URL and its join token before switching Internet access on")
            changed = sorted(k for k in new if new[k] != self.settings.get(k))
            if not changed:
                return self.status()
            old = self.settings
            self.settings = merged
            save_settings(merged)
            reconnect = any(k in changed for k in ("url", "token", "name", "enabled"))
            drop = "url" in changed or ("enabled" in changed and not merged["enabled"])
            self.agent.configure(self._agent_cfg(), reconnect=reconnect, close_channels=drop)
        what = []
        for k in changed:
            if k == "token":
                what.append("join token")
            elif k == "enabled":
                what.append("on" if merged["enabled"] else "off")
            elif k == "ports":
                what.append("ports %s" % ("any" if merged["ports"] is None else ",".join(map(str, merged["ports"]))))
            else:
                what.append("%s %s -> %s" % (k, old.get(k) or "-", merged.get(k) or "-"))
        log.info("internet access settings changed by session %s (%s): %s", getattr(session, "num", "?"),
                 getattr(session, "controller", "?"), "; ".join(what))
        self.publish()
        return self.status()

    def op_relay_key(self, session, _m):
        with self.lock:
            key = self.identity["key"]
        log.info("access key shown to session %s (%s)", getattr(session, "num", "?"), getattr(session, "controller", "?"))
        return {"key": key, "key_id": e2e.key_id(e2e.parse_key(key))}

    def op_relay_rotate(self, session, _m):
        with self.lock:
            ident = dict(self.identity, key=e2e.new_key())
            _write_private(identity_path(), ident)
            self.identity = ident
            self.agent.configure(self._agent_cfg(), reconnect=False)
        closed = self.agent.close_channels("the access key was replaced")
        kid = e2e.key_id(e2e.parse_key(ident["key"]))
        log.info("access key replaced by session %s (%s): new key id %s, %d viewer(s) disconnected",
                 getattr(session, "num", "?"), getattr(session, "controller", "?"), kid, closed)
        self.publish()
        return {"key": ident["key"], "key_id": kid, "closed": closed}

    def op_relay_kick(self, session, _m):
        closed = self.agent.close_channels("disconnected from this machine")
        log.info("viewers disconnected by session %s (%s): %d", getattr(session, "num", "?"),
                 getattr(session, "controller", "?"), closed)
        self.publish()
        return {"closed": closed}


def main(argv=None):
    """`python3 -m arstro_remote.relay.service key`: print this slot's access key (made if there is
    none yet). install.sh uses it; ARSTRO_SLOT / XDG_CONFIG_HOME select the slot."""
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["key"]:
        print("usage: python3 -m arstro_remote.relay.service key", file=sys.stderr)
        return 2
    print(load_identity()["key"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
