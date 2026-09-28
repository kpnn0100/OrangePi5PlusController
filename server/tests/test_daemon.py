#!/usr/bin/env python3
"""End-to-end tests against a running daemon over its local control socket.

Run on the Orange Pi:
    python3 tests/test_daemon.py [--wifi-live]

--wifi-live also disconnects and reconnects the active Wi-Fi profile (the link drops
for a few seconds; the test restores it). Needs the daemon running in the desktop
session (polkit), i.e. started by the autostart launcher.
"""

import argparse
import os
import re
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from arstro_remote.client import Client  # noqa: E402
from arstro_remote.daemon import control_socket_path  # noqa: E402
from arstro_remote.protocol import (FrameDecoder, ProtocolError, T_JSON, T_TERM,  # noqa: E402
                                    encode_json, encode_term)

RESULTS = []


def test(fn):
    RESULTS.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def term_text(c, tid):
    return bytes(c.term_output.get(tid, b"")).decode("utf-8", "replace")


def wait_term(c, tid, pattern, timeout=8):
    ok = c.wait_for(lambda: re.search(pattern, term_text(c, tid)) is not None, timeout)
    check(ok, "terminal output never matched %r; got tail: %r" % (pattern, term_text(c, tid)[-300:]))


# ----------------------------------------------------------------- protocol
@test
def protocol_codec():
    d = FrameDecoder()
    data = encode_json({"a": 1}) + encode_term(3, b"xyz") + encode_json({"b": "é"})
    frames = []
    for i in range(len(data)):  # byte-by-byte feeding
        frames += d.feed(data[i:i + 1])
    check(frames[0] == (T_JSON, b'{"a":1}'), frames)
    check(frames[1] == (T_TERM, b"\x03xyz"), frames)
    check(len(frames) == 3, frames)
    try:
        FrameDecoder().feed(b"\x09\x00\x00\x00\x01x")
        raise AssertionError("bad frame type accepted")
    except ProtocolError:
        pass
    try:
        FrameDecoder().feed(b"\x01\x7f\x00\x00\x00")
        raise AssertionError("oversize frame accepted")
    except ProtocolError:
        pass


@test
def hello_and_ping(c):
    h = c.call("hello", app="test", version="0")
    check(h["proto"] == 1 and h["name"] == "Arstro Remote", h)
    check(set(["stats", "wifi", "terminal", "input"]) <= set(h["features"]), h)
    check(h["input"]["available"], "input backend unavailable: %s" % h["input"])
    p = c.call("ping", t=123)
    check(p["echo"] == 123, p)


@test
def unknown_op_is_error(c):
    r = c.request("nope.nothing")
    check(r["ok"] is False and "unknown op" in r["error"], r)
    r = c.request("term.resize", term=99, cols=10, rows=10)
    check(r["ok"] is False, r)


# -------------------------------------------------------------------- stats
@test
def stats_snapshot(c):
    s = c.call("stats.get")
    for key in ("hostname", "uptime", "cpu", "memory", "temps", "network", "disks", "wifi", "processes"):
        check(key in s, "missing %s" % key)
    check(len(s["cpu"]["per_core"]) == 8, s["cpu"])
    check("soc" in s["temps"] and 10 < s["temps"]["soc"] < 110, s["temps"])
    check(s["cpu_temp"] is not None, "no cpu temp")
    check(s["network"]["ip"] and re.match(r"\d+\.\d+\.\d+\.\d+", s["network"]["ip"]), s["network"])
    check(s["memory"]["total"] > 1 << 30, s["memory"])
    check(s["disks"] and s["disks"][0]["mount"] == "/", s["disks"])
    print("      ip=%s cpu=%s%% soc=%sC mem=%s%% wifi=%s gpu=%s fan=%s" % (
        s["network"]["ip"], s["cpu"]["percent"], s["temps"]["soc"], s["memory"]["percent"],
        s["wifi"].get("ssid"), s["devfreq"].get("gpu"), s["fan_percent"]))


@test
def stats_subscription(c):
    c.events.clear()
    r = c.call("stats.subscribe", interval_ms=500)
    check(r["interval_ms"] == 500, r)
    ok = c.wait_for(lambda: sum(1 for e in c.events if e.get("ev") == "stats") >= 3, 5)
    check(ok, "expected >=3 stats events, got %d" % sum(1 for e in c.events if e.get("ev") == "stats"))
    c.call("stats.unsubscribe")
    time.sleep(0.7)
    n = sum(1 for e in c.events if e.get("ev") == "stats")
    time.sleep(1.5)
    check(sum(1 for e in c.events if e.get("ev") == "stats") == n, "events continued after unsubscribe")
    # net rates are computed between samples
    s = c.call("stats.get")
    check(any("rx_rate" in i for i in s["network"]["interfaces"]), "no rx_rate")


# ----------------------------------------------------------------- terminal
@test
def terminal_basic(c):
    tid = c.call("term.open", cols=100, rows=30)["term"]
    c.term_write(tid, b"echo ARSTRO_$((6*7))\n")
    wait_term(c, tid, r"ARSTRO_42")
    c.term_write(tid, b"stty size; echo $TERM\n")
    wait_term(c, tid, r"30 100\r?\nxterm-256color")
    c.call("term.resize", term=tid, cols=132, rows=43)
    c.term_write(tid, b"stty size\n")
    wait_term(c, tid, r"43 132")
    c.term_write(tid, "echo unicode-ệñ中\n".encode())
    wait_term(c, tid, "unicode-ệñ中")
    # Ctrl+C interrupts a foreground job
    c.term_write(tid, b"sleep 30; echo NOT_INTERRUPTED\n")
    time.sleep(0.5)
    c.term_write(tid, b"\x03")
    c.term_write(tid, b"echo AFTER_$((1+1))\n")
    wait_term(c, tid, r"AFTER_2")
    check("NOT_INTERRUPTED" not in term_text(c, tid).split("sleep 30")[-1].replace("echo NOT_INTERRUPTED", ""),
          "Ctrl+C did not interrupt")
    # big output is streamed completely
    c.term_output[tid] = bytearray()
    c.term_write(tid, b"seq 1 20000 | tail -c 100000 >/dev/null; seq 1 20000; echo SEQ_DONE\n")
    wait_term(c, tid, r"20000\r?\nSEQ_DONE", timeout=20)
    # exit -> term.exit event
    c.events.clear()
    c.term_write(tid, b"exit 3\n")
    ok = c.wait_for(lambda: any(e.get("ev") == "term.exit" and e.get("term") == tid for e in c.events), 5)
    check(ok, "no term.exit event")
    ev = next(e for e in c.events if e.get("ev") == "term.exit")
    check(ev["code"] == 3, ev)


@test
def terminal_multiple_and_close(c):
    a = c.call("term.open", cols=80, rows=24)["term"]
    b = c.call("term.open", cols=80, rows=24)["term"]
    check(a != b, (a, b))
    c.term_write(a, b"echo TERM_A\n")
    c.term_write(b, b"echo TERM_B\n")
    wait_term(c, a, "TERM_A")
    wait_term(c, b, "TERM_B")
    check("TERM_B" not in term_text(c, a), "output leaked between terminals")
    c.term_write(b, b"echo $$ > /tmp/arstro_term_pid\n")
    time.sleep(0.5)
    pid = int(open("/tmp/arstro_term_pid").read())
    c.call("term.close", term=b)
    time.sleep(0.5)
    check(not os.path.exists("/proc/%d" % pid), "shell %d still alive after close" % pid)
    c.call("term.close", term=a)


@test
def terminal_closed_with_session():
    c2 = Client.unix(control_socket_path())
    tid = c2.call("term.open")["term"]
    c2.term_write(tid, b"echo $$ > /tmp/arstro_term_pid2\n")
    time.sleep(0.6)
    pid = int(open("/tmp/arstro_term_pid2").read())
    c2.close()
    time.sleep(1.0)
    check(not os.path.exists("/proc/%d" % pid), "shell survived disconnect")


@test
def terminal_persists_across_reconnect():
    cid = "test-%d" % int(time.time() * 1000)
    c1 = Client.unix(control_socket_path())
    c1.call("hello", app="test", client_id=cid)
    tid = c1.call("term.open", cols=80, rows=24)["term"]
    c1.term_write(tid, b"X=persisted; echo BEFORE_DROP; sleep 1; echo WHILE_DETACHED\n")
    wait_term(c1, tid, r"BEFORE_DROP\r?\n")
    time.sleep(0.2)
    have = len(c1.term_output[tid])
    c1.close()  # link drops while the shell is still busy
    time.sleep(2.0)
    c2 = Client.unix(control_socket_path())
    h = c2.call("hello", app="test", client_id=cid)
    mine = [t for t in h["terminals"] if t["term"] == tid]
    check(mine and not mine[0]["attached"], "detached terminal not listed: %s" % h["terminals"])
    r = c2.call("term.attach", term=tid, cols=90, rows=20, since=have)
    check(r["gap"] is False and r["start"] == have and r["replayed"] > 0, r)
    wait_term(c2, tid, "WHILE_DETACHED")  # produced while nobody was attached
    check("BEFORE_DROP" not in term_text(c2, tid), "exact replay repeated old output: %r" % term_text(c2, tid))
    # a client with no offset gets the whole buffer and gap=true
    c2b = Client.unix(control_socket_path())
    c2b.call("hello", app="test", client_id=cid)
    r = c2b.call("term.attach", term=tid)
    check(r["gap"] is True and r["start"] == 0, r)
    wait_term(c2b, tid, "WHILE_DETACHED")
    check("BEFORE_DROP" in term_text(c2b, tid), "full replay incomplete")
    c2b.close()
    time.sleep(0.5)
    r = c2.call("term.attach", term=tid, since=have + r["replayed"] - 0)  # take it back
    c2.term_write(tid, b"echo VAR=$X; stty size\n")
    wait_term(c2, tid, r"VAR=persisted")
    wait_term(c2, tid, r"20 90")
    c3 = Client.unix(control_socket_path())
    c3.call("hello", app="test", client_id=cid + "-other")
    check(tid not in [t["term"] for t in c3.call("term.list")["terminals"]], "other client sees terminal")
    check(c3.request("term.attach", term=tid)["ok"] is False, "other client attached")
    c3.close()
    c2.call("term.close", term=tid)
    check(tid not in [t["term"] for t in c2.call("term.list")["terminals"]], "closed terminal still listed")
    c2.close()


# -------------------------------------------------------------------- input
@test
def mouse_motion(c):
    c.call("in.move_to", x=300, y=300)
    p = c.call("in.pointer")
    check((p["x"], p["y"]) == (300, 300), p)
    c.notify("in.move", dx=40, dy=-25)
    for _ in range(4):
        c.notify("in.move", dx=0.25, dy=0.5)  # fractions accumulate -> +1, +2
    time.sleep(0.3)
    p = c.call("in.pointer")
    check((p["x"], p["y"]) == (341, 277), p)
    check(p["width"] >= 640 and p["height"] >= 480, p)


class Xev:
    """Runs xev in a window and collects the events it sees."""

    def __init__(self):
        self.proc = subprocess.Popen(
            ["xev", "-geometry", "700x500+300+200", "-name", "arstro-test", "-event", "keyboard",
             "-event", "button", "-event", "focus"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":0")))
        time.sleep(1.5)

    def stop(self):
        self.proc.terminate()
        out, _ = self.proc.communicate(timeout=5)
        return out


def xev_keys(out):
    """Characters produced by KeyPress events (XLookupString) + keysym names."""
    chars, syms = [], []
    for block in out.split("\n\n"):
        if not block.startswith("KeyPress"):
            continue
        m = re.search(r"keysym (0x[0-9a-f]+), (\S+)\)", block)
        if m:
            syms.append(m.group(2))
        m = re.search(r'XLookupString gives \d+ bytes: (?:\(\w+\) )*"(.*)"', block)
        if m and m.group(1):
            chars.append(m.group(1))
    return "".join(chars), syms


@test
def keyboard_and_buttons(c):
    xev = Xev()
    try:
        c.call("in.move_to", x=650, y=450)
        c.notify("in.btn", b="left", a="click")
        time.sleep(0.5)
        c.notify("in.text", s="Hello, World! 123 ~@#$%^&*()_+{}|:\"<>?")
        c.notify("in.key", k="Return")
        c.notify("in.key", k="a", mods=["ctrl"])
        c.notify("in.key", k="F5")
        c.notify("in.text", s="việt ñ")
        c.notify("in.btn", b="right", a="click")
        c.notify("in.btn", b="middle", a="down")
        c.notify("in.btn", b="middle", a="up")
        c.notify("in.scroll", dy=2)
        c.notify("in.scroll", dy=-1)
        c.notify("in.scroll", dx=1)
        c.call("ping")  # everything before this has been processed in order
        time.sleep(1.0)
    finally:
        out = xev.stop()
    text, syms = xev_keys(out)
    check("Hello, World! 123 ~@#$%^&*()_+{}|:\"<>?" in text, "typed text wrong: %r" % text)
    check("Return" in syms, syms)
    ctrl_a = re.search(r"KeyPress event.*?state 0x4,.*?keysym 0x61, a\)", out, re.S)
    check(ctrl_a is not None, "ctrl+a not seen with Control state")
    check("F5" in syms, syms)
    check("ecircumflexbelowdot" in syms or "0x1001ec7" in out or "ệ" in text,
          "unicode char not typed: syms=%s" % syms[-6:])
    check("ntilde" in syms, syms[-6:])
    buttons = re.findall(r"ButtonPress event.*?button (\d+)", out, re.S)
    check(buttons[:1] == ["1"], buttons)
    check(buttons.count("3") == 1 and buttons.count("2") == 1, buttons)
    check(buttons.count("5") == 2 and buttons.count("4") == 1 and buttons.count("7") == 1, buttons)
    releases = re.findall(r"ButtonRelease event.*?button (\d+)", out, re.S)
    check(releases.count("2") == 1, releases)


@test
def held_buttons_released_on_disconnect():
    c2 = Client.unix(control_socket_path())
    c2.call("in.move_to", x=50, y=50)
    c2.notify("in.btn", b="left", a="down")
    c2.notify("in.key", k="Shift_L", a="down")
    c2.call("ping")
    p = c2.call("in.pointer")
    check(p["mask"] & 0x100, "button1 not held: mask=%x" % p["mask"])
    c2.close()
    time.sleep(0.8)
    c3 = Client.unix(control_socket_path())
    p = c3.call("in.pointer")
    c3.close()
    check(not p["mask"] & 0x101, "button/shift still held after disconnect: mask=%x" % p["mask"])


@test
def input_helper_respawns_daemon_survives(c):
    """An X crash only kills the input helper, never the daemon or its shells."""
    daemon_pid = c.call("admin.status")["pid"]
    tid = c.call("term.open")["term"]
    c.call("in.pointer")  # make sure the helper runs
    pids = subprocess.run(["pgrep", "-f", "arstro_remote.input_x11 --serve"],
                          capture_output=True, text=True).stdout.split()
    check(pids, "no input helper process")
    for p in pids:
        os.kill(int(p), 9)
    deadline = time.time() + 8
    ok = False
    while time.time() < deadline:
        r = c.request("in.pointer")
        if r["ok"]:
            ok = True
            break
        time.sleep(0.5)
    check(ok, "input did not recover after helper was killed")
    check(c.call("admin.status")["pid"] == daemon_pid, "daemon restarted")
    c.term_write(tid, b"echo STILL_ALIVE\n")
    wait_term(c, tid, "STILL_ALIVE")
    c.call("term.close", term=tid)


@test
def bad_input_rejected(c):
    r = c.request("in.key", k="NoSuchKeyName")
    check(r["ok"] is False and "unknown key" in r["error"], r)
    r = c.request("in.btn", b="nose")
    check(r["ok"] is False, r)


# --------------------------------------------------------------------- wifi
@test
def wifi_status_scan_saved(c):
    st = c.call("wifi.status")
    check(st["device"], st)
    check(st["enabled"] is True, st)
    check(st["connected"] and st["ssid"], st)
    sc = c.call("wifi.scan", rescan=True, timeout=60)
    check(sc["networks"], "no networks")
    cur = [n for n in sc["networks"] if n["in_use"]]
    check(cur and cur[0]["ssid"] == st["ssid"], cur)
    check(len({n["ssid"] for n in sc["networks"]}) == len(sc["networks"]), "duplicate SSIDs")
    saved = c.call("wifi.saved")["networks"]
    check(any(n["ssid"] == st["ssid"] and n["active"] for n in saved), saved)
    print("      connected to %s (%s%%), %d networks visible, %d saved" % (
        st["ssid"], st["signal"], len(sc["networks"]), len(saved)))


@test
def wifi_connect_errors(c):
    st = c.call("wifi.status")
    r = c.call("wifi.connect", ssid=st["ssid"])
    check("Already connected" in r["message"], r)
    r = c.request("wifi.connect", ssid="arstro-no-such-network-xyz", password="12345678", timeout=90)
    check(r["ok"] is False and "not found" in r["error"].lower(), r)
    r = c.request("wifi.connect", ssid="")
    check(r["ok"] is False, r)
    saved = c.call("wifi.saved")["networks"]
    check(not any(n["ssid"] == "arstro-no-such-network-xyz" for n in saved), "failed profile left behind")
    st2 = c.call("wifi.status")
    check(st2["connected"] and st2["ssid"] == st["ssid"], "lost Wi-Fi: %s" % st2)


def wifi_live_reconnect(c):
    st = c.call("wifi.status")
    ssid = st["ssid"]
    print("      disconnecting from %s ..." % ssid)
    try:
        c.call("wifi.disconnect")
        time.sleep(2)
        st2 = c.call("wifi.status")
        check(not st2["connected"], "still connected after disconnect: %s" % st2)
        r = c.call("wifi.connect", ssid=ssid, timeout=90)
        check("Connected" in r["message"] and r["status"]["connected"], r)
        print("      %s, ip %s" % (r["message"], r["status"]["ip"]))
    finally:
        if not c.call("wifi.status")["connected"]:
            c.request("wifi.connect", ssid=ssid, timeout=90)


@test
def admin_status(c):
    st = c.call("admin.status")
    check(st["bluetooth"]["ready"], st["bluetooth"])
    check(st["bluetooth"]["uuid"] == "a57e0001-7c2b-4d1e-9f3a-5e7a1b2c3d4e", st["bluetooth"])
    check(any(s["local"] for s in st["sessions"]), st["sessions"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wifi-live", action="store_true")
    ap.add_argument("-k", help="only run tests whose name contains this")
    args = ap.parse_args()
    tests = list(RESULTS)
    if args.wifi_live:
        tests.append(wifi_live_reconnect)
    if args.k:
        tests = [t for t in tests if args.k in t.__name__]
    c = Client.unix(control_socket_path())
    passed = failed = 0
    for t in tests:
        t0 = time.time()
        try:
            if t.__code__.co_argcount:
                t(c)
            else:
                t()
            passed += 1
            print("PASS  %-36s %.1fs" % (t.__name__, time.time() - t0))
        except Exception as e:
            failed += 1
            print("FAIL  %-36s %s" % (t.__name__, e))
            traceback.print_exc(limit=3)
    c.close()
    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
