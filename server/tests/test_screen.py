#!/usr/bin/env python3
"""Remote screen tests (SCR-01..05): the Pi desktop streamed and controlled.

Run on the Pi (server running, desktop session on the display):
    python3 tests/test_screen.py

Only moves the pointer (no clicks, no typing), and puts it back.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

from harness import check, nal_types, run, test, wait_until

from arstro_remote.client import Client
from arstro_remote.paths import control_socket_path, load_config
from arstro_remote.web import auth, ws

HERE = os.path.dirname(os.path.abspath(__file__))


class Ctx:
    pass


def setup():
    c = Ctx()
    c.a = Client.unix(control_socket_path())
    c.a.call("hello", app="test-screen")
    c.b = Client.unix(control_socket_path())
    c.b.call("hello", app="test-observer")
    c.port = int(load_config().get("web_port", 8080))
    c.token = auth.load()
    st = c.a.call("screen.status")
    c.orig = {k: st[k] for k in ("quality", "encoder", "bitrate")}
    c.has_vpu = subprocess.run(["gst-inspect-1.0", "mpph264enc"], capture_output=True).returncode == 0
    c.orig_pointer = c.a.call("in.pointer")
    return c


def teardown(c):
    try:
        c.a.call("screen.settings.set", settings=c.orig)
        c.a.call("in.move_to", x=c.orig_pointer["x"], y=c.orig_pointer["y"])
    finally:
        c.a.close()
        c.b.close()


def viewer(c):
    return ws.connect("ws://127.0.0.1:%d/ws/screen" % c.port, {"Authorization": "Bearer " + c.token})


def watch(conn, seconds):
    """-> (config, [(t, key, data)])"""
    cfg, frames, t0 = None, [], time.monotonic()
    while time.monotonic() - t0 < seconds:
        op, data = conn.recv_message()
        if op == ws.OP_TEXT:
            m = json.loads(data)
            if m.get("type") == "config":
                cfg = m
            continue
        frames.append((time.monotonic() - t0, bool(data[1] & 1), data[10:]))
    return cfg, frames


@test("SCR-01", "SCR-03")
def desktop_is_streamed_live(c):
    st = c.a.call("screen.status")
    check(st["available"], "no desktop: %s" % st)
    conn = viewer(c)
    try:
        cfg, frames = watch(conn, 5)
    finally:
        conn.close()
    check(cfg and cfg["width"] > 0 and cfg["height"] > 0, "no config: %s" % cfg)
    check(frames and frames[0][0] < 2.0, "first frame after %s s" % (frames[0][0] if frames else None))
    key = next((f for f in frames if f[1]), None)
    check(key and {7, 8, 5} <= set(nal_types(key[2])), "keyframe without SPS/PPS/IDR")
    fps = len(frames) / (5 - frames[0][0])
    gaps = sorted(b[0] - a[0] for a, b in zip(frames, frames[1:]))
    p95 = gaps[int(len(gaps) * 0.95)]
    vpu = cfg.get("encoder") == "vpu"
    print("      %sx%s, %.1f fps, 95%% gap %.0f ms, %s" % (cfg["width"], cfg["height"], fps, p95 * 1000, cfg.get("encoder")))
    check(fps >= (15 if vpu else 20), "only %.1f fps" % fps)
    check(p95 < (0.25 if vpu else 0.15), "stutters: 95%% gap %.0f ms" % (p95 * 1000))
    st = c.a.call("screen.status")
    check(st["screen"] and st["screen"][0] >= cfg["width"], st)


@test("SCR-01")
def capture_runs_only_while_watched(c):
    conn = viewer(c)
    try:
        watch(conn, 1)
        check(wait_until(lambda: c.a.call("screen.status")["state"] == "live", 5), "not live")
    finally:
        conn.close()
    check(wait_until(lambda: c.a.call("screen.status")["state"] == "stopped", 12), "capture still running 12 s later")
    out = subprocess.run(["pgrep", "-f", "^" + sys.executable.split("/")[-1] + " -m arstro_remote.screen.worker"],
                         capture_output=True, text=True).stdout.strip()
    check(not out, "screen worker still alive: %s" % out)


@test("SCR-03", "ARC-03")
def quality_is_shared_and_applied(c):
    c.a.call("screen.settings.set", settings={"quality": "low"})
    check(c.b.wait_state("screen", lambda s: s["quality"] == "low", 2), "observer did not get the new quality")
    conn = viewer(c)
    try:
        cfg, frames = watch(conn, 3)
    finally:
        conn.close()
    check(cfg and cfg["width"] <= 960 and cfg["fps"] == 15, cfg)
    check(frames, "no frames at low quality")
    r = c.a.request("screen.settings.set", settings={"quality": "ultra"})
    check(r["ok"] is False, r)


@test("SCR-05", "ARC-03")
def encoder_and_bitrate_are_shared_and_applied(c):
    c.a.call("screen.settings.set", settings={"quality": "medium", "encoder": "cpu", "bitrate": 1_000_000})
    check(c.b.wait_state("screen", lambda s: s["encoder"] == "cpu" and s["bitrate"] == 1_000_000, 2),
          "observer did not get the encoder / bitrate")
    encoders = ["cpu", "vpu"] if c.has_vpu else ["cpu"]
    for enc in encoders:
        c.a.call("screen.settings.set", settings={"encoder": enc})
        conn = viewer(c)
        try:
            cfg, frames = watch(conn, 4)
        finally:
            conn.close()
        check(cfg and cfg.get("encoder") == enc and cfg["bitrate"] == 1_000_000, cfg)
        key = next((f for f in frames if f[1]), None)
        check(key and {7, 8, 5} <= set(nal_types(key[2])), "%s: no keyframe with SPS/PPS/IDR" % enc)
        span = frames[-1][0] - frames[0][0] if len(frames) > 1 else 0
        kbps = sum(len(f[2]) for f in frames) * 8 / max(span, 0.1) / 1000
        print("      %s: %.1f fps, %.0f kbit/s (asked 1000)" % (enc, len(frames) / max(span, 0.1), kbps))
        check(kbps < 2500, "%s ignores the bitrate: %.0f kbit/s" % (enc, kbps))
    c.a.call("screen.settings.set", settings={"bitrate": 0})
    check(c.a.call("screen.status")["bitrate"] == 0, "bitrate auto not taken")
    for bad in ({"bitrate": 5}, {"bitrate": "lots"}, {"encoder": "gpu"}, {}):
        r = c.a.request("screen.settings.set", settings=bad)
        check(r["ok"] is False and r.get("error"), "accepted %s: %s" % (bad, r))


@test("SCR-02")
def pointer_goes_where_the_viewer_points(c):
    st = c.a.call("screen.status")
    w, h = st["screen"] or (1024, 768)
    for fx, fy in ((0.25, 0.25), (0.75, 0.6)):
        x, y = int(w * fx), int(h * fy)
        c.a.call("in.move_to", x=x, y=y)
        p = c.a.call("in.pointer")
        check(abs(p["x"] - x) <= 1 and abs(p["y"] - y) <= 1, "pointer %s, wanted %s" % (p, (x, y)))


@test("SCR-04", "SCR-05", "CON-04")
def cli_status_and_save(c):
    env = dict(os.environ)
    out = subprocess.run([sys.executable, "-m", "arstro_remote", "screen", "status"], cwd=os.path.join(HERE, ".."),
                         capture_output=True, text=True, timeout=30, env=env)
    check(out.returncode == 0 and "Desktop" in out.stdout and "encoder" in out.stdout, out.stdout + out.stderr)
    for args, want in ((["encoder", "cpu"], ("encoder", "cpu")), (["bitrate", "1.5M"], ("bitrate", 1_500_000)),
                       (["bitrate", "auto"], ("bitrate", 0))):
        out = subprocess.run([sys.executable, "-m", "arstro_remote", "screen"] + args, cwd=os.path.join(HERE, ".."),
                             capture_output=True, text=True, timeout=30, env=env)
        check(out.returncode == 0 and c.a.call("screen.status")[want[0]] == want[1], out.stdout + out.stderr)
    path = tempfile.mktemp(suffix=".h264")
    try:
        out = subprocess.run([sys.executable, "-m", "arstro_remote", "screen", "save", path, "--seconds", "2"],
                             cwd=os.path.join(HERE, ".."), capture_output=True, text=True, timeout=60, env=env)
        check(out.returncode == 0 and os.path.getsize(path) > 1000, out.stdout + out.stderr)
        probe = subprocess.run(["ffprobe", "-v", "error", "-f", "h264", "-count_packets", "-show_entries",
                                "stream=width,height,nb_read_packets", "-of", "csv=p=0", path],
                               capture_output=True, text=True, timeout=30).stdout.strip()
        print("      saved clip: %s" % probe)
        check(probe and int(probe.split(",")[-1]) >= 20, probe)
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, __doc__))
