#!/usr/bin/env python3
"""The machine's own Wi-Fi network (WIFI-08), against a throwaway server and a fake nmcli.

A real hotspot takes the machine off its Wi-Fi network (one radio) - on a board reached over
Wi-Fi that cuts the test off - so this suite puts `fake_nmcli.py` first on PATH: it keeps
NetworkManager's state in a file and records every call, so the test checks what the server
asked NetworkManager to do (an access point profile, never at boot, shared IPv4, WPA2) as well
as what every controller sees. Runs anywhere:

    python3 tests/test_hotspot.py
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

from harness import check, run, test, wait_until

from instance import SERVER, Instance

FAKE = tempfile.mkdtemp(prefix="arstro-nm-")
STATE = os.path.join(FAKE, "state.json")
PROC = os.path.join(FAKE, "proc")                  # the server's /proc/net (ARSTRO_PROC_NET)
UDP_HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
# a dnsmasq service with the stock configuration: DNS on every address
DNS_EVERYWHERE = "   0: 00000000:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000   108        0 4711 2\n"


def proc_udp(rows=""):
    with open(os.path.join(PROC, "udp"), "w") as f:
        f.write(UDP_HEAD + rows)


def nm():
    with open(STATE) as f:
        return json.load(f)


def nm_set(**kw):
    s = nm()
    s.update(kw)
    with open(STATE, "w") as f:
        json.dump(s, f)


def profile(name="Arstro Hotspot"):
    return next((c for c in nm()["cons"].values() if c["name"] == name), None)


def cli(t, *args):
    r = subprocess.run([sys.executable, "-m", "arstro_remote", "--slot", "t", *args], cwd=SERVER, env=t.env,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def setup():
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_nmcli.py"), os.path.join(FAKE, "nmcli"))
    os.chmod(os.path.join(FAKE, "nmcli"), 0o755)
    os.environ["PATH"] = FAKE + os.pathsep + os.environ["PATH"]
    os.environ["FAKE_NM_STATE"] = STATE
    os.makedirs(PROC)
    proc_udp("   1: 0100007F:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000   108        0 4712 2\n")
    os.environ["ARSTRO_PROC_NET"] = PROC              # DNS on 127.0.0.1 only: no conflict
    t = Instance(["system", "connection"], config={"wifi_poll_sec": 3})
    t.start()
    t.c.call("wifi.status")
    return t


def teardown(t):
    t.stop()
    shutil.rmtree(FAKE, ignore_errors=True)


def wifi_state(t):
    return t.c.state.get("wifi") or {}


@test("WIFI-08")
def hotspot_is_off_with_defaults(t):
    h = t.c.call("wifi.hotspot")
    check(h["supported"] and h["active"] is False and h["device"] == "wlan0", h)
    check(h["ssid"].startswith("Arstro-") and h["band"] == "2.4 GHz", h)
    check(h["password"] is None and h["has_password"] is False, h)
    check(h["connected_to"] == "Home", h)


@test("WIFI-08", "WIFI-07", "SEC-05")
def start_makes_an_access_point_and_every_controller_sees_it(t):
    r = t.c.call("wifi.hotspot.start", ssid="Arstro-test", timeout=60)
    h = r["hotspot"]
    pw = h["password"]
    check(h["active"] and h["ssid"] == "Arstro-test" and h["ip"] == "10.42.0.1", h)
    check(h["web"] == "http://10.42.0.1:%d/" % t.port, h)
    check(len(pw) == 12 and pw.isalnum() and pw == pw.lower() and not set(pw) & set("01loi"), "password %r" % pw)
    p = profile()["props"]
    check(p["802-11-wireless.mode"] == "ap" and p["connection.autoconnect"] == "no", p)
    check(p["ipv4.method"] == "shared" and p["wifi-sec.key-mgmt"] == "wpa-psk" and p["wifi-sec.proto"] == "rsn", p)
    check(p["802-11-wireless-security.psk"] == pw and p["802-11-wireless.band"] == "bg", p)
    # pushed to every controller (ARC-03) - without the password
    check(wait_until(lambda: (wifi_state(t).get("hotspot") or {}).get("active"), 5), wifi_state(t))
    st = wifi_state(t)
    check(st["connected"] is False and st["hotspot"]["ssid"] == "Arstro-test", st)
    check(st["hotspot"]["web"] == h["web"], st)
    check(pw not in json.dumps(st) and pw not in json.dumps(t.c.call("wifi.status")), "password in the state")
    # kept 0600, shared by the slots; never in the log (SEC-05)
    f = os.path.join(t.dir, "data", "arstro-remote", "hotspot.json")
    check(stat.S_IMODE(os.stat(f).st_mode) == 0o600, oct(os.stat(f).st_mode))
    with open(t.log) as lf:
        log = lf.read()
    check("Arstro-test" in log and pw not in log, "log: %s" % log[-600:])
    # the CLI shows it, with the password (it asked for it)
    rc, out = cli(t, "wifi", "hotspot")
    check(rc == 0 and "Hotspot  : on" in out and pw in out and "10.42.0.1" in out, out)
    rc, out = cli(t, "wifi", "status")
    check(rc == 0 and "hotspot Arstro-test on" in out, out)


@test("WIFI-08")
def settings_are_kept_and_checked_before_anything_changes(t):
    pw = t.c.call("wifi.hotspot")["password"]
    r = t.c.call("wifi.hotspot.start", band="5", timeout=60)     # restart on 5 GHz: name and password kept
    check(r["hotspot"]["ssid"] == "Arstro-test" and r["hotspot"]["password"] == pw, r)
    check(r["hotspot"]["band"] == "5 GHz" and profile()["props"]["802-11-wireless.band"] == "a", r)
    before = profile()["props"]
    for kw, words in (({"password": "short"}, "8 to 63"), ({"ssid": "x" * 33}, "1 to 32"),
                      ({"band": "7"}, "2.4 or 5"), ({"password": "pässwort-mit-umlaut"}, "ASCII")):
        resp = t.c.request("wifi.hotspot.start", **kw)
        check(resp["ok"] is False and words in resp["error"], (kw, resp))
    check(profile()["props"] == before, "a refused start changed the profile")
    t.c.call("wifi.hotspot.start", password="my own pass 1", band="2.4", timeout=60)
    h = t.c.call("wifi.hotspot")
    check(h["password"] == "my own pass 1" and h["band"] == "2.4 GHz", h)


@test("WIFI-08", "WIFI-07")
def stop_goes_back_to_the_saved_network(t):
    r = t.c.call("wifi.hotspot.stop", timeout=60)
    check(r["message"] == "Hotspot off, back on Home" and r["status"]["connected"], r)
    check(wait_until(lambda: wifi_state(t).get("connected") and not wifi_state(t).get("hotspot"), 5), wifi_state(t))
    check(wifi_state(t)["ssid"] == "Home", wifi_state(t))
    check(t.c.call("wifi.hotspot.stop")["message"] == "The hotspot is not on", "second stop")
    check(t.c.call("wifi.hotspot")["active"] is False, "still active")


@test("WIFI-08")
def a_failed_start_returns_to_the_network(t):
    nm_set(fail_up=True)
    resp = t.c.request("wifi.hotspot.start", timeout=60)
    check(resp["ok"] is False and "did not start" in resp["error"], resp)
    st = t.c.call("wifi.status")
    check(st["connected"] and st["ssid"] == "Home" and not st.get("hotspot"), st)


@test("WIFI-08")
def a_hotspot_that_goes_down_at_once_is_a_failure_not_a_success(t):
    nm_set(drop_after_up=True)                        # NetworkManager: "activated", then down
    resp = t.c.request("wifi.hotspot.start", band="5", timeout=60)
    check(resp["ok"] is False and "went down as it started" in resp["error"], resp)
    check("try 2.4 GHz" in resp["error"], "no 5 GHz hint: %s" % resp["error"])
    st = t.c.call("wifi.status")
    check(st["connected"] and st["ssid"] == "Home" and not st.get("hotspot"), st)
    t.c.call("wifi.hotspot.start", band="2.4", timeout=60)     # (keeps 2.4 GHz for the next tests)
    t.c.call("wifi.hotspot.stop", timeout=60)


@test("WIFI-08")
def a_dns_server_on_every_address_is_named_before_the_network_is_left(t):
    proc_udp(DNS_EVERYWHERE)
    try:
        h = t.c.call("wifi.hotspot")
        check(h["problem"] and "port 53" in h["problem"] and "setup_hotspot_dns.sh" in h["problem"], h)
        calls = len(nm()["calls"])
        resp = t.c.request("wifi.hotspot.start", timeout=60)
        check(resp["ok"] is False and "cannot start" in resp["error"] and "dnsmasq" in resp["error"], resp)
        ran = [c for c in nm()["calls"][calls:] if "up" in c or "modify" in c or "add" in c]
        check(not ran, "the network was touched: %s" % ran)
        check(t.c.call("wifi.status")["ssid"] == "Home", "left the network")
        rc, out = cli(t, "wifi", "hotspot")
        check("port 53" in out, out)
    finally:
        proc_udp()
    check(t.c.call("wifi.hotspot")["problem"] is None, "problem stays after the fix")


@test("WIFI-08")
def the_cli_asks_before_leaving_the_network(t):
    rc, out = cli(t, "wifi", "hotspot", "on")
    check(rc != 0 and "leaves Home" in out and "--yes" in out, out)
    check(t.c.call("wifi.hotspot")["active"] is False, "started without --yes")
    rc, out = cli(t, "wifi", "hotspot", "on", "--yes")
    check(rc == 0 and "Hotspot Arstro-test is on" in out and "web: http://10.42.0.1:%d/" % t.port in out, out)
    rc, out = cli(t, "wifi", "hotspot", "off")
    check(rc == 0 and "back on Home" in out, out)


@test("WIFI-08", "ARC-02")
def the_web_api_runs_the_same_ops(t):
    code, body = t.http("POST", "/api/op/wifi.hotspot", b"{}", {"Content-Type": "application/json"})
    d = json.loads(body)
    check(code == 200 and d["ok"] and d["data"]["ssid"] == "Arstro-test", (code, body[:300]))


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, description=__doc__))
