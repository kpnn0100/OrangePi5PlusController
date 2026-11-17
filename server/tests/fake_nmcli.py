#!/usr/bin/env python3
"""A stand-in for NetworkManager's `nmcli`, enough for the Wi-Fi and hotspot ops (WIFI-08).

Installed as `nmcli` in a directory put first on PATH; its state is a JSON file
(`FAKE_NM_STATE`). One Wi-Fi device `wlan0`, one saved network `Home` it is connected to.
Every call is appended to the state's `calls` (so a test can check what the server ran);
`fail_up` makes the next `connection up` of an access point fail like a radio that refuses,
`drop_after_up` makes it succeed and the access point go down a moment later.
"""

import fcntl
import json
import os
import sys
import uuid as uuidlib

DEV = "wlan0"
STATE = os.environ["FAKE_NM_STATE"]


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        home = "11111111-2222-3333-4444-555555555555"
        return {"radio": True, "active": home, "fail_up": False, "calls": [],
                "cons": {home: {"name": "Home", "props": {"802-11-wireless.ssid": "Home",
                                                          "802-11-wireless.mode": "infrastructure",
                                                          "802-11-wireless.band": "",
                                                          "connection.autoconnect": "yes"}}}}


def esc(v):
    return str(v).replace("\\", "\\\\").replace(":", "\\:")


def main(argv):
    st = load()
    st["calls"].append(argv)
    terse = fields = getf = None
    args = list(argv)
    while args and args[0].startswith("-"):
        a = args.pop(0)
        if a == "-t":
            terse = True
        elif a == "-f":
            fields = args.pop(0).split(",")
        elif a == "-g":
            getf = args.pop(0).split(",")
        elif a == "--wait":
            args.pop(0)
    out, rc = [], 0
    cons, active = st["cons"], st["active"]
    con = cons.get(active) if active else None
    ap = bool(con and con["props"].get("802-11-wireless.mode") == "ap")

    def find(ref):
        if ref in cons:
            return ref
        return next((u for u, c in cons.items() if c["name"] == ref), None)

    def props(words):
        d = {}
        for k, v in zip(words[::2], words[1::2]):
            k = {"ssid": "802-11-wireless.ssid", "wifi-sec.psk": "802-11-wireless-security.psk"}.get(k, k)
            d[k] = v
        return d

    if args[:2] == ["radio", "wifi"]:
        if len(args) > 2:
            st["radio"] = args[2] == "on"
        else:
            out.append("enabled" if st["radio"] else "disabled")
    elif args == ["device"]:
        out.append("%s:wifi:%s" % (DEV, "connected" if con else "disconnected"))
        out.append("lo:loopback:connected (externally)")
    elif args[:2] == ["device", "show"]:
        want = getf or fields or []
        vals = {"GENERAL.STATE": "100 (connected)" if con else "30 (disconnected)",
                "GENERAL.CONNECTION": con["name"] if con else "",
                "IP4.ADDRESS[1]": ("10.42.0.1/24" if ap else "192.0.2.10/24") if con else None,
                "WIFI-PROPERTIES.AP": "yes"}
        for k in want:
            for key, v in vals.items():
                if v is not None and (key == k or key.startswith(k + "[")):
                    out.append(v if getf else "%s:%s" % (key, v))
    elif args[:3] == ["device", "wifi", "list"]:
        if con and not ap:
            out.append("yes:%s:70" % esc(con["props"]["802-11-wireless.ssid"]))
        out.append("no:Neighbour:40")
    elif args[:2] == ["device", "connect"]:
        if st["radio"]:
            st["active"] = next((u for u, c in cons.items() if c["props"].get("connection.autoconnect") == "yes"
                                 and c["props"].get("802-11-wireless.mode") != "ap"), None)
        if not st["active"]:
            out.append("Error: no network to connect to")
            rc = 4
    elif args[:2] == ["device", "disconnect"]:
        st["active"] = None
    elif args[:2] == ["connection", "show"]:
        if "--active" in args:
            if con:
                out.append("%s:802-11-wireless:%s" % (active, DEV))
        elif len(args) > 2:
            u = find(args[2])
            if not u:
                out.append("Error: %s - no such connection profile." % args[2])
                rc = 10
            else:
                for k in getf or []:
                    out.append(cons[u]["props"].get(k, ""))
        else:
            for u, c in cons.items():
                row = {"NAME": c["name"], "UUID": u, "TYPE": "802-11-wireless",
                       "AUTOCONNECT": c["props"].get("connection.autoconnect", "yes"),
                       "ACTIVE": "yes" if u == active else "no", "DEVICE": DEV if u == active else ""}
                out.append(":".join(esc(row[f]) for f in fields))
    elif args[:2] == ["connection", "add"]:
        words = args[2:]
        name = words[words.index("con-name") + 1]
        rest = [w for i, w in enumerate(words) if i not in (words.index("type"), words.index("type") + 1,
                                                            words.index("con-name"), words.index("con-name") + 1)]
        u = str(uuidlib.uuid4())
        cons[u] = {"name": name, "props": props(rest)}
        out.append("Connection '%s' (%s) successfully added." % (name, u))
    elif args[:2] == ["connection", "modify"]:
        u = find(args[2])
        cons[u]["props"].update(props(args[3:]))
    elif args[:2] == ["connection", "up"]:
        u = find(args[2])
        if cons[u]["props"].get("802-11-wireless.mode") == "ap" and st["fail_up"]:
            st["fail_up"] = False
            st["active"] = None
            out.append("Error: Connection activation failed: 802.1X supplicant took too long to authenticate.")
            rc = 4
        elif cons[u]["props"].get("802-11-wireless.mode") == "ap" and st.get("drop_after_up"):
            st["drop_after_up"] = False     # "activated", then down a moment later (its dnsmasq failed)
            st["active"] = None
        else:
            st["active"] = u
    elif args[:2] == ["connection", "down"]:
        if find(args[2]) == active:
            st["active"] = None
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE + ".tmp", STATE)
    print("\n".join(out), file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    with open(STATE + ".lock", "w") as lock:       # the server runs nmcli from several threads
        fcntl.flock(lock, fcntl.LOCK_EX)
        sys.exit(main(sys.argv[1:]))
