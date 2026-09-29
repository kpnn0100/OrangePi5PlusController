"""Connection module (NET-01..05): network devices and connection profiles through
NetworkManager (nmcli) - Ethernet addressing, any saved profile - and Bluetooth devices
through BlueZ's bluetoothctl. Wi-Fi scanning/joining stays in wifi.py (WIFI-*).
"""

import ipaddress
import logging
import re
import subprocess
import threading

from .session import OpError
from .wifi import WifiError, _nmcli, split_terse

log = logging.getLogger("arstro.net")

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
IPV4_METHODS = ("auto", "manual", "disabled", "shared", "link-local")
PROFILE_FIELDS = ["connection.id", "connection.uuid", "connection.type", "connection.interface-name",
                  "connection.autoconnect", "ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns",
                  "ipv4.never-default", "ipv6.method", "802-3-ethernet.mtu"]


def _nm(args, timeout=30):
    try:
        return _nmcli(args, timeout=timeout)
    except WifiError as e:
        raise OpError(str(e))


# ----------------------------------------------------------------- NetworkManager
def devices():
    out = []
    for line in _nm(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]).splitlines():
        f = split_terse(line)
        if len(f) < 4 or f[1] in ("loopback",) or f[0].startswith(("veth", "docker", "br-", "virbr")):
            continue
        d = {"device": f[0], "type": f[1], "state": f[2], "connection": f[3] or None,
             "hwaddr": None, "mtu": None, "ip4": [], "gateway": None, "dns": [], "carrier": None}
        try:
            info = _nm(["-t", "-f", "GENERAL.HWADDR,GENERAL.MTU,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,"
                        "WIRED-PROPERTIES.CARRIER", "device", "show", f[0]], timeout=10) \
                if f[1] == "ethernet" else \
                _nm(["-t", "-f", "GENERAL.HWADDR,GENERAL.MTU,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
                     "device", "show", f[0]], timeout=10)
        except OpError:
            info = ""
        for row in info.splitlines():
            key, _, value = row.partition(":")
            value = value.replace("\\:", ":")
            if key == "GENERAL.HWADDR":
                d["hwaddr"] = value or None
            elif key == "GENERAL.MTU":
                d["mtu"] = int(value) if value.isdigit() else None
            elif key.startswith("IP4.ADDRESS") and value:
                d["ip4"].append(value)
            elif key == "IP4.GATEWAY":
                d["gateway"] = value or None
            elif key.startswith("IP4.DNS") and value:
                d["dns"].append(value)
            elif key == "WIRED-PROPERTIES.CARRIER":
                d["carrier"] = value == "on"
        out.append(d)
    return out


def connections():
    out = []
    for line in _nm(["-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE,AUTOCONNECT", "connection", "show"]).splitlines():
        f = split_terse(line)
        if len(f) < 6 or f[2] == "loopback":
            continue
        out.append({"name": f[0], "uuid": f[1], "type": f[2], "device": f[3] or None,
                    "active": f[4] == "yes", "autoconnect": f[5] == "yes"})
    return out


def _uuid(u):
    u = str(u or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", u):
        raise OpError("a connection UUID is required")
    return u


def connection(uuid):
    out = _nm(["-t", "-f", ",".join(PROFILE_FIELDS), "connection", "show", _uuid(uuid)])
    d = {}
    for row in out.splitlines():
        key, _, value = row.partition(":")
        if key in PROFILE_FIELDS:
            d[key] = value.replace("\\:", ":")
    return {"name": d.get("connection.id"), "uuid": d.get("connection.uuid"), "type": d.get("connection.type"),
            "interface": d.get("connection.interface-name") or None,
            "autoconnect": d.get("connection.autoconnect") == "yes",
            "ipv4": {"method": d.get("ipv4.method"),
                     "addresses": [a.strip() for a in (d.get("ipv4.addresses") or "").split(",") if a.strip()],
                     "gateway": d.get("ipv4.gateway") if d.get("ipv4.gateway") not in (None, "", "--") else None,
                     "dns": [a.strip() for a in (d.get("ipv4.dns") or "").split(",") if a.strip()],
                     "never_default": d.get("ipv4.never-default") == "yes"},
            "ipv6": {"method": d.get("ipv6.method")},
            "mtu": d.get("802-3-ethernet.mtu")}


def _ipv4_args(msg, method_default=None):
    """nmcli modify/add arguments for the ipv4 fields present in msg (validated)."""
    args = []
    method = msg.get("ipv4_method", method_default)
    addresses = msg.get("addresses")
    if isinstance(addresses, str):
        addresses = [a.strip() for a in re.split(r"[,\s]+", addresses) if a.strip()]
    if method is not None:
        if method not in IPV4_METHODS:
            raise OpError("ipv4 method must be one of %s" % ", ".join(IPV4_METHODS))
        if method == "manual" and not addresses:
            raise OpError("a static address (like 192.0.2.50/24) is needed for manual addressing")
    if addresses is not None:
        for a in addresses:
            try:
                ipaddress.IPv4Interface(a if "/" in a else a + "/24")
            except ValueError:
                raise OpError("%r is not an IPv4 address (use 192.0.2.50/24)" % a)
        addresses = [a if "/" in a else a + "/24" for a in addresses]
    gateway = msg.get("gateway")
    if gateway:
        try:
            ipaddress.IPv4Address(gateway)
        except ValueError:
            raise OpError("%r is not an IPv4 gateway address" % gateway)
    dns = msg.get("dns")
    if isinstance(dns, str):
        dns = [a.strip() for a in re.split(r"[,\s]+", dns) if a.strip()]
    for a in dns or []:
        try:
            ipaddress.ip_address(a)
        except ValueError:
            raise OpError("%r is not a DNS server address" % a)
    if method is not None:
        args += ["ipv4.method", method]
    if method in ("auto", "disabled", "link-local", "shared") and addresses is None:
        args += ["ipv4.addresses", "", "ipv4.gateway", ""]
    if addresses is not None:
        args += ["ipv4.addresses", ",".join(addresses)]
    if gateway is not None:
        args += ["ipv4.gateway", gateway or ""]
    if dns is not None:
        args += ["ipv4.dns", ",".join(dns)]
    return args


def modify(uuid, msg):
    args = _ipv4_args(msg)
    if "autoconnect" in msg:
        args += ["connection.autoconnect", "yes" if msg["autoconnect"] else "no"]
    if msg.get("name"):
        args += ["connection.id", str(msg["name"])]
    if "interface" in msg:
        args += ["connection.interface-name", msg["interface"] or ""]
    if msg.get("mtu") not in (None, ""):
        args += ["802-3-ethernet.mtu", str(int(msg["mtu"]))]
    if not args:
        raise OpError("nothing to change")
    _nm(["connection", "modify", _uuid(uuid)] + args)
    return connection(uuid)


def add_ethernet(msg):
    ifname = msg.get("interface")
    if not ifname:
        raise OpError("the Ethernet interface is required")
    name = msg.get("name") or "%s static" % ifname
    args = ["connection", "add", "type", "ethernet", "ifname", ifname, "con-name", name]
    args += _ipv4_args(msg, method_default="auto")
    out = _nm(args)
    m = re.search(r"\(([0-9a-f-]{36})\)", out)
    return connection(m.group(1)) if m else {"message": out.strip()}


def up(uuid, timeout=45):
    _nm(["--wait", str(timeout), "connection", "up", _uuid(uuid)], timeout=timeout + 10)
    return {"message": "activated"}


def down(uuid):
    _nm(["connection", "down", _uuid(uuid)])
    return {"message": "deactivated"}


def delete(uuid):
    _nm(["connection", "delete", _uuid(uuid)])
    return {"message": "deleted"}


def device_connect(dev, on=True):
    if not re.fullmatch(r"[\w.:-]{1,32}", str(dev or "")):
        raise OpError("bad device name")
    _nm(["--wait", "30", "device", "connect" if on else "disconnect", dev], timeout=40)
    return {"message": ("connected " if on else "disconnected ") + dev}


# ---------------------------------------------------------------------- Bluetooth
def _btctl(args, timeout=15):
    cmd = ["bluetoothctl"] + args
    log.debug("run %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except FileNotFoundError:
        raise OpError("bluetoothctl not found (BlueZ missing)")
    except subprocess.TimeoutExpired:
        raise OpError("bluetoothctl timed out")
    out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
    return r.returncode, out


def _mac(a):
    a = str(a or "").strip().upper()
    if not MAC_RE.match(a):
        raise OpError("a Bluetooth address like AA:BB:CC:DD:EE:FF is required")
    return a


def bt_status():
    rc, out = _btctl(["show"], 8)
    if rc != 0 or "Controller" not in out:
        return {"present": False, "error": out.strip().splitlines()[-1] if out.strip() else "no adapter"}
    d = {"present": True}
    m = re.search(r"Controller ([0-9A-F:]{17})", out)
    d["address"] = m.group(1) if m else None
    for key in ("Name", "Alias", "Powered", "Discoverable", "Pairable", "Discovering"):
        m = re.search(r"^\s*%s: (.*)$" % key, out, re.M)
        if m:
            v = m.group(1).strip()
            d[key.lower()] = (v == "yes") if v in ("yes", "no") else v
    return d


def bt_power(on):
    rc, out = _btctl(["power", "on" if on else "off"])
    if rc != 0 or "succeeded" not in out:
        raise OpError("could not switch Bluetooth %s: %s" % ("on" if on else "off", out.strip()[-200:]))
    return bt_status()


def _info(mac):
    rc, out = _btctl(["info", mac], 8)
    d = {"address": mac}
    for key in ("Name", "Alias", "Icon", "Paired", "Bonded", "Trusted", "Blocked", "Connected", "RSSI"):
        m = re.search(r"^\s*%s: (.*)$" % key, out, re.M)
        if m:
            v = m.group(1).strip()
            d[key.lower()] = (v == "yes") if v in ("yes", "no") else v
    if "rssi" in d:
        m = re.search(r"-?\d+", str(d["rssi"]))
        d["rssi"] = int(m.group(0)) if m else None
    return d


def bt_devices():
    rc, out = _btctl(["devices"], 8)
    devs = []
    for m in re.finditer(r"^Device ([0-9A-F:]{17}) ?(.*)$", out, re.M):
        d = _info(m.group(1))
        d.setdefault("name", m.group(2) or m.group(1))
        devs.append(d)
    devs.sort(key=lambda d: (not d.get("connected"), not d.get("paired"), str(d.get("name")).lower()))
    return devs


def bt_scan(seconds=8):
    seconds = max(3, min(int(seconds), 30))
    _btctl(["--timeout", str(seconds), "scan", "on"], seconds + 5)
    return bt_devices()


def bt_action(action, mac, timeout=30):
    mac = _mac(mac)
    if action not in ("pair", "connect", "disconnect", "trust", "untrust", "remove"):
        raise OpError("unknown Bluetooth action %s" % action)
    rc, out = _btctl(["--timeout", str(timeout), action, mac], timeout)
    ok_words = {"pair": ("Pairing successful", "AlreadyExists"), "connect": ("Connection successful",),
                "disconnect": ("Successful disconnected", "Disconnection successful", "Not Connected"),
                "trust": ("trust succeeded",), "untrust": ("untrust succeeded",), "remove": ("Device has been removed",)}
    if not any(w.lower() in out.lower() for w in ok_words[action]):
        tail = [l for l in out.strip().splitlines() if l.strip()][-2:]
        raise OpError("%s %s failed: %s" % (action, mac, " / ".join(tail) or "no answer"))
    return _info(mac) if action != "remove" else {"address": mac, "removed": True}


class ConnectionService:
    """net.* and bt.* ops (wifi.* are Session ops); publishes the topic "net"."""

    def __init__(self, ctx):
        self.ctx = ctx

    def start(self):
        pass

    def shutdown(self):
        pass

    def refresh(self):
        try:
            data = {"devices": devices(), "connections": connections()}
        except OpError as e:
            data = {"error": str(e)}
        self.ctx.hub.publish("net", data)
        return data

    def handle(self, session, op, msg):
        who = "session %d (%s)" % (session.num, session.controller)
        if op == "net.status":
            return self.refresh()
        if op == "net.devices":
            return {"devices": devices()}
        if op == "net.connections":
            return {"connections": connections()}
        if op == "net.connection.get":
            return connection(msg.get("uuid"))
        changing = {"net.connection.set": lambda: modify(msg.get("uuid"), msg),
                    "net.connection.add_ethernet": lambda: add_ethernet(msg),
                    "net.connection.up": lambda: up(msg.get("uuid")),
                    "net.connection.down": lambda: down(msg.get("uuid")),
                    "net.connection.delete": lambda: delete(msg.get("uuid")),
                    "net.device.connect": lambda: device_connect(msg.get("device"), True),
                    "net.device.disconnect": lambda: device_connect(msg.get("device"), False)}
        if op in changing:
            log.info("%s %s by %s", op, {k: v for k, v in msg.items() if k not in ("op", "id")}, who)
            try:
                return changing[op]()
            finally:
                self.refresh()
                try:
                    self.ctx.wifi_refresh()
                except Exception:
                    pass
        if op == "bt.status":
            return bt_status()
        if op == "bt.power":
            log.info("bluetooth power %s by %s", bool(msg.get("on", True)), who)
            return bt_power(bool(msg.get("on", True)))
        if op == "bt.devices":
            return {"devices": bt_devices()}
        if op == "bt.scan":
            return {"devices": bt_scan(msg.get("seconds", 8))}
        if op in ("bt.pair", "bt.connect", "bt.disconnect", "bt.trust", "bt.untrust", "bt.remove"):
            log.info("%s %s by %s", op, msg.get("address"), who)
            r = bt_action(op[3:], msg.get("address"))
            if self.ctx.bt:
                threading.Thread(target=self.ctx.publish_pairing, daemon=True).start()
            return r
        raise OpError("unknown op %s" % op)
