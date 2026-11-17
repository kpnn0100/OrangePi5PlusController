"""Wi-Fi control through NetworkManager's nmcli.

nmcli needs polkit's network-control permission. On this board that is granted to
processes inside the active desktop session, which is why the daemon is started
from the XFCE autostart and not from a systemd user unit.
"""

import json
import logging
import os
import secrets
import socket
import subprocess

from . import paths

log = logging.getLogger("arstro.wifi")

WIFI_TYPE = "802-11-wireless"


class WifiError(Exception):
    pass


def split_terse(line: str):
    """Split one line of `nmcli -t` output. Fields are ':'-separated and a literal
    ':' or '\\' inside a field is escaped with a backslash."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def _nmcli(args, timeout=30, check=True):
    cmd = ["nmcli"] + args
    shown = []
    hide_next = False
    for a in cmd:  # never log passwords
        shown.append("***" if hide_next else a)
        hide_next = a in ("password", "wifi-sec.psk")
    log.debug("run %s", " ".join(shown))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WifiError("nmcli timed out after %ds" % timeout)
    except FileNotFoundError:
        raise WifiError("nmcli not found (NetworkManager missing)")
    if check and res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        if msg.startswith("Error: "):
            msg = msg[len("Error: "):]
        raise WifiError(msg or "nmcli failed with code %d" % res.returncode)
    return res.stdout


def wifi_device():
    """Name of the first Wi-Fi interface, or None."""
    for line in _nmcli(["-t", "-f", "DEVICE,TYPE,STATE", "device"]).splitlines():
        f = split_terse(line)
        if len(f) >= 2 and f[1] == "wifi":
            return f[0]
    return None


def _require_device(ifname=None):
    dev = ifname or wifi_device()
    if not dev:
        raise WifiError("No Wi-Fi device found")
    return dev


def radio_enabled():
    return _nmcli(["radio", "wifi"]).strip() == "enabled"


def set_radio(enabled: bool):
    _nmcli(["radio", "wifi", "on" if enabled else "off"])
    return {"enabled": radio_enabled()}


def saved_networks():
    """Saved Wi-Fi connection profiles."""
    out = _nmcli(["-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT,ACTIVE,DEVICE", "connection", "show"])
    nets = []
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 6 or f[2] != WIFI_TYPE:
            continue
        ssid = f[0]
        try:
            ssid = _nmcli(["-g", "802-11-wireless.ssid", "connection", "show", f[1]], timeout=10).strip() or f[0]
        except WifiError:
            pass
        nets.append({"name": f[0], "uuid": f[1], "ssid": ssid, "autoconnect": f[3] == "yes",
                     "active": f[4] == "yes", "device": f[5] or None})
    return nets


def status():
    dev = wifi_device()
    info = {"device": dev, "enabled": radio_enabled(), "connected": False,
            "ssid": None, "signal": None, "ip": None, "state": None, "connection": None}
    if not dev:
        return info
    out = _nmcli(["-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", dev])
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key == "GENERAL.STATE":
            info["state"] = value.split("(")[-1].rstrip(")") if "(" in value else value
            info["connected"] = value.startswith("100")
        elif key == "GENERAL.CONNECTION":
            info["connection"] = value or None
        elif key.startswith("IP4.ADDRESS") and not info["ip"]:
            info["ip"] = value.split("/")[0] or None
    hs = _ap_profile(dev) if info["connected"] else None
    if hs:                          # the machine runs a hotspot: it is not on a network (WIFI-08)
        info["connected"] = False
        info["hotspot"] = dict(hs, active=True, ip=info["ip"], clients=_clients(dev))
        return info
    if info["connected"]:
        for line in _nmcli(["-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list",
                            "ifname", dev, "--rescan", "no"]).splitlines():
            f = split_terse(line)
            if len(f) >= 3 and f[0] == "yes":
                info["ssid"] = f[1]
                info["signal"] = int(f[2] or 0)
                break
    return info


def scan(rescan=True):
    dev = _require_device()
    out = _nmcli(["-t", "-f", "IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY", "device", "wifi", "list",
                  "ifname", dev, "--rescan", "yes" if rescan else "auto"], timeout=40)
    saved = {n["ssid"] for n in saved_networks()}
    best = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 7 or not f[2]:
            continue  # hidden network without SSID
        in_use = f[0].strip() == "*"
        try:
            signal = int(f[5] or 0)
        except ValueError:
            signal = 0
        net = {"ssid": f[2], "bssid": f[1], "channel": f[3], "freq": f[4], "signal": signal,
               "security": f[6].strip() or "", "in_use": in_use, "saved": f[2] in saved}
        cur = best.get(f[2])
        if cur is None or in_use or (not cur["in_use"] and signal > cur["signal"]):
            if cur is not None and cur["in_use"] and not in_use:
                continue
            best[f[2]] = net
    nets = sorted(best.values(), key=lambda n: (not n["in_use"], -n["signal"]))
    return {"device": dev, "networks": nets}


def connect(ssid, password=None, hidden=False, timeout=45):
    if not ssid:
        raise WifiError("SSID is required")
    dev = _require_device()
    current = status()
    if current["connected"] and current["ssid"] == ssid and not password:
        return {"message": "Already connected to %s" % ssid, "status": current}

    profile = next((n for n in saved_networks() if n["ssid"] == ssid), None)
    if profile:
        if password:
            _nmcli(["connection", "modify", profile["uuid"],
                    "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password])
        try:
            _nmcli(["--wait", str(timeout), "connection", "up", profile["uuid"], "ifname", dev],
                   timeout=timeout + 10)
        except WifiError as e:
            raise WifiError(_friendly(str(e)))
    else:
        args = ["--wait", str(timeout), "device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        if hidden:
            args += ["hidden", "yes"]
        args += ["ifname", dev]
        try:
            _nmcli(args, timeout=timeout + 10)
        except WifiError as e:
            _delete_failed_profile(ssid)
            raise WifiError(_friendly(str(e)))
    st = status()
    if not st["connected"]:
        raise WifiError("Activation finished but %s is not connected" % dev)
    return {"message": "Connected to %s" % (st["ssid"] or ssid), "status": st}


def _delete_failed_profile(ssid):
    """nmcli may leave a half-created, never-activated profile behind."""
    try:
        for n in saved_networks():
            if n["ssid"] == ssid and not n["active"]:
                _nmcli(["connection", "delete", n["uuid"]], check=False)
    except WifiError:
        pass


def _friendly(msg):
    low = msg.lower()
    if "secrets were required" in low or "802-1x" in low or ("psk" in low and "invalid" in low):
        return "Wrong password or authentication failed (%s)" % msg
    if "no network with ssid" in low:
        return "Network not found. Is it in range? (%s)" % msg
    if "not authorized" in low:
        return "Not authorized by polkit - the service must run in the desktop session (%s)" % msg
    return msg


def disconnect():
    dev = _require_device()
    _nmcli(["device", "disconnect", dev])
    return {"message": "Disconnected %s" % dev}


def forget(uuid):
    if not uuid:
        raise WifiError("uuid is required")
    _nmcli(["connection", "delete", uuid])
    return {"message": "Forgot network"}


# ------------------------------------------------------------------ hotspot (WIFI-08)
HOTSPOT_PROFILE = "Arstro Hotspot"      # the NetworkManager profile the server owns
BANDS = {"bg": "2.4 GHz", "a": "5 GHz"}
_PASSWORD_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i: easy to type on a phone
ARP = "/proc/net/arp"
_profiles = {}                          # uuid -> {mode, ssid, band}: profiles rarely change


def _active_uuid(dev):
    for line in _nmcli(["-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active"]).splitlines():
        f = split_terse(line)
        if len(f) >= 3 and f[2] == dev and f[1] == WIFI_TYPE:
            return f[0]
    return None


def _profile(uuid):
    if uuid not in _profiles:
        mode, band = (_nmcli(["-g", "802-11-wireless.mode,802-11-wireless.band", "connection", "show", uuid],
                             timeout=10).split("\n") + ["", ""])[:2]
        ssid = _nmcli(["-g", "802-11-wireless.ssid", "connection", "show", uuid], timeout=10).strip()
        _profiles[uuid] = {"mode": mode.strip(), "band": band.strip() or None, "ssid": ssid or None}
    return _profiles[uuid]


def _ap_profile(dev):
    """{ssid, band, profile} of the access point `dev` runs now, or None."""
    try:
        uuid = _active_uuid(dev)
        p = _profile(uuid) if uuid else None
    except WifiError as e:
        log.debug("hotspot check: %s", e)
        return None
    if not p or p["mode"] != "ap":
        return None
    return {"ssid": p["ssid"], "band": BANDS.get(p["band"], "auto"), "profile": uuid}


def _clients(dev):
    """Devices that joined the hotspot: complete entries of the kernel's ARP table on `dev`."""
    try:
        with open(ARP) as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return None
    return sum(1 for r in rows if len(r.split()) >= 6 and r.split()[5] == dev and r.split()[2] == "0x2")


def hotspot_file():
    """Name, password and band of the hotspot - one file for every slot of the machine, 0600."""
    return os.path.join(paths.data_dir(), "hotspot.json")


def hotspot_settings():
    try:
        with open(hotspot_file()) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    host = socket.gethostname().split(".")[0] or "machine"
    return {"ssid": d.get("ssid") or ("Arstro-" + host)[:32], "password": d.get("password") or None,
            "band": d.get("band") if d.get("band") in BANDS else "bg"}


def _save_hotspot_settings(d):
    path = hotspot_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _band(value):
    v = str(value or "").strip().lower().replace(" ", "").replace("ghz", "")
    if v in ("", "none"):
        return None
    if v in ("bg", "2.4", "2", "24"):
        return "bg"
    if v in ("a", "5"):
        return "a"
    raise WifiError("Band must be 2.4 or 5 (GHz)")


def _check(ssid, password):
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise WifiError("The hotspot name must be 1 to 32 bytes long")
    if password is not None:
        if not 8 <= len(password) <= 63:
            raise WifiError("The hotspot password must be 8 to 63 characters long")
        if any(ord(ch) < 32 or ord(ch) > 126 for ch in password):
            raise WifiError("The hotspot password may only use plain ASCII letters, digits and symbols")


def _supports_ap(dev):
    try:
        return _nmcli(["-g", "WIFI-PROPERTIES.AP", "device", "show", dev], timeout=10).strip() != "no"
    except WifiError:
        return True                     # unknown: let NetworkManager decide


def hotspot(with_password=False):
    """The hotspot's settings and whether it runs now (`wifi.hotspot`)."""
    dev = wifi_device()
    st = _settings_public(hotspot_settings(), with_password)
    st.update({"device": dev, "supported": bool(dev) and _supports_ap(dev), "active": False,
               "ip": None, "clients": None})
    if dev:
        live = status()
        if live.get("hotspot"):
            st.update({k: v for k, v in live["hotspot"].items() if v is not None})
        st["connected_to"] = live.get("ssid") if live.get("connected") else None
    return st


def _settings_public(s, with_password):
    out = {"ssid": s["ssid"], "band": BANDS[s["band"]], "has_password": bool(s["password"])}
    if with_password:
        out["password"] = s["password"]
    return out


def hotspot_start(ssid=None, password=None, band=None, timeout=40):
    """Start (or restart with new settings) the machine's own Wi-Fi network."""
    dev = _require_device()
    if not _supports_ap(dev):
        raise WifiError("The Wi-Fi device %s cannot run a hotspot (no access point mode)" % dev)
    cur = hotspot_settings()
    ssid = (ssid or "").strip() or cur["ssid"]
    password = password or cur["password"] or "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(12))
    band = _band(band) or cur["band"]
    _check(ssid, password)
    if not radio_enabled():
        set_radio(True)
    uuid = next((n["uuid"] for n in saved_networks() if n["name"] == HOTSPOT_PROFILE), None)
    common = ["802-11-wireless.ssid", ssid, "802-11-wireless.mode", "ap", "802-11-wireless.band", band,
              "connection.interface-name", dev, "connection.autoconnect", "no",
              "ipv4.method", "shared", "ipv6.method", "ignore",
              "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
              "wifi-sec.group", "ccmp", "wifi-sec.psk", password]
    if uuid:
        _nmcli(["connection", "modify", uuid] + common)
    else:
        _nmcli(["connection", "add", "type", "wifi", "con-name", HOTSPOT_PROFILE] + common)
        uuid = next((n["uuid"] for n in saved_networks() if n["name"] == HOTSPOT_PROFILE), None)
    _profiles.pop(uuid, None)
    _save_hotspot_settings({"ssid": ssid, "password": password, "band": band})
    try:
        _nmcli(["--wait", str(timeout), "connection", "up", uuid or HOTSPOT_PROFILE, "ifname", dev],
               timeout=timeout + 10)
    except WifiError as e:
        _nmcli(["--wait", "30", "device", "connect", dev], timeout=40, check=False)   # back to its network
        raise WifiError("The hotspot did not start: %s" % _friendly(str(e)))
    log.info("hotspot %r on %s (%s)", ssid, dev, BANDS[band])
    return {"message": "Hotspot %s is on" % ssid, "hotspot": hotspot(with_password=True)}


def hotspot_stop(timeout=30):
    """Stop the hotspot; the machine reconnects to its saved networks."""
    dev = _require_device()
    hs = _ap_profile(dev)
    if not hs:
        return {"message": "The hotspot is not on", "hotspot": hotspot()}
    _nmcli(["connection", "down", hs["profile"]])
    _nmcli(["--wait", str(timeout), "device", "connect", dev], timeout=timeout + 10, check=False)
    st = status()
    log.info("hotspot %r off; Wi-Fi now: %s", hs["ssid"], st.get("ssid") or st.get("state"))
    back = (", back on %s" % st["ssid"]) if st.get("connected") and st.get("ssid") else \
        " - no saved network in range" if not st.get("connected") else ""
    return {"message": "Hotspot off" + back, "status": st, "hotspot": hotspot()}
