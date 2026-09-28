"""Wi-Fi control through NetworkManager's nmcli.

nmcli needs polkit's network-control permission. On this board that is granted to
processes inside the active desktop session, which is why the daemon is started
from the XFCE autostart and not from a systemd user unit.
"""

import logging
import subprocess

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
