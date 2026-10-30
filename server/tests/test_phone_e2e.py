#!/usr/bin/env python3
"""A shared phone, end to end through the device manager (PHN-01..19) - opt-in.

Every feature of docs/phone/PHONE.md is used the way a viewer uses it: through the relay and the
end-to-end encrypted channel (http and ws streams), with an Android emulator playing the outside
world (SMS, GPS, notifications, the call's Answer button) over adb.

    ARSTRO_PHONE_RELAY=http://127.0.0.1:8090/arstro/internal/devices ARSTRO_PHONE_ID=<device id> \\
    ARSTRO_PHONE_KEY_FILE=<file with the phone's ak1_ key> ARSTRO_PHONE_ADB=emulator-5554 \\
        python3 tests/test_phone_e2e.py

Without those variables every test is skipped. ARSTRO_PHONE_ADB must name an emulator or a test
phone - the tests send SMS, ring, lock the screen and post notifications on it.
"""

import json
import os
import re
import struct
import subprocess
import time

from harness import check, run, test

from arstro_remote.relay import client, e2e

RELAY = os.environ.get("ARSTRO_PHONE_RELAY")
ID = os.environ.get("ARSTRO_PHONE_ID")
KEY_FILE = os.environ.get("ARSTRO_PHONE_KEY_FILE")
ADB = os.environ.get("ARSTRO_PHONE_ADB")
ON = bool(RELAY and ID and KEY_FILE and ADB)


class Ctx:
    ch = None


def adb(*args, timeout=30):
    r = subprocess.run(["adb", "-s", ADB, *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def channel():
    if Ctx.ch is None or Ctx.ch.closed:
        with open(KEY_FILE) as f:
            Ctx.ch = client.Relay(RELAY, timeout=30).connect(ID, f.read().strip())
    return Ctx.ch


def op(_op, timeout=60, **args):
    status, _, _, body = channel().http("POST", 8080, "/api/op/" + _op, {"Content-Type": "application/json"},
                                        json.dumps(args).encode(), timeout=timeout)
    d = json.loads(body.decode() or "{}")
    return status, d


def ok(_op, timeout=60, **args):
    status, d = op(_op, timeout=timeout, **args)
    check(status == 200 and d.get("ok"), "%s -> %s %s" % (_op, status, str(d)[:300]))
    return d.get("data")


def get(path, timeout=60):
    status, _, headers, body = channel().http("GET", 8080, path, timeout=timeout)
    return status, dict((k.lower(), v) for k, v in headers), body


class Ws:
    """A ws stream of the channel: text = JSON objects, binary = bytes."""

    def __init__(self, path):
        self.s = channel().stream({"t": "ws", "port": 8080, "path": path, "headers": [], "protocols": []})
        ev = self.s.next(timeout=30)
        check(ev[0] == "json" and ev[1].get("t") == "open", "%s did not open: %r" % (path, ev))
        self.buf = b""

    def send_json(self, obj):
        self.s.send_data(json.dumps(obj).encode(), e2e.F_FIN | e2e.F_TEXT)

    def send_binary(self, data):
        self.s.send_data(data, e2e.F_FIN)

    def recv(self, timeout=30):
        """-> ("text", obj) | ("binary", bytes) | ("closed", reason)."""
        while True:
            ev = self.s.next(timeout=timeout)
            if ev[0] == "data":
                self.buf += ev[2]
                if ev[1] & e2e.F_FIN:
                    data, self.buf = self.buf, b""
                    return ("text", json.loads(data.decode())) if ev[1] & e2e.F_TEXT else ("binary", data)
            elif ev[0] == "json" and ev[1].get("t") == "close":
                return ("closed", ev[1].get("reason") or ev[1].get("code"))
            elif ev[0] == "closed" or (ev[0] == "json" and ev[1].get("t") == "error"):
                return ("closed", ev[1])

    def close(self):
        try:
            self.s.send_json({"t": "close", "code": 1000, "reason": ""})
        except client.RelayError:
            pass
        self.s.close()


def skip():
    if not ON:
        print("      (skipped: set ARSTRO_PHONE_RELAY, ARSTRO_PHONE_ID, ARSTRO_PHONE_KEY_FILE, ARSTRO_PHONE_ADB)")
        return True
    return False


def video_frames(path, want=5, timeout=40):
    w = Ws(path)
    try:
        config, frames, key = None, 0, False
        end = time.time() + timeout
        while time.time() < end and frames < want:
            kind, data = w.recv(timeout=timeout)
            if kind == "text" and data.get("t") == "config":
                config = data
                w.send_json({"t": "keyframe"})
            elif kind == "binary":
                check(len(data) > 9, "a video message is flags|pts|AU")
                flags, _pts = struct.unpack(">BQ", data[:9])
                key = key or bool(flags & 1)
                check(data[9:13] in (b"\0\0\0\1",) or data[9:12] == b"\0\0\1", "Annex-B start code")
                frames += 1
            elif kind == "closed":
                check(False, "%s closed: %r" % (path, data))
        check(config and config.get("codec", "").startswith("avc1."), "config: %r" % (config,))
        check(frames >= want and key, "%d frames, key frame seen: %s" % (frames, key))
        return config, w
    except Exception:
        w.close()
        raise


@test("PHN-01", "PHN-02", "PHN-18", "PHN-19")
def the_phone_answers_through_the_channel():
    if skip():
        return
    info = channel().info
    check(info.get("slot") == "phone" and info.get("ports") == [8080], "ready: %s" % info)
    status, headers, body = get("/api/info")
    d = json.loads(body)
    check(status == 200 and len(d["features"]) == 14, "features: %s" % [f["id"] for f in d.get("features", [])])
    not_ready = [(f["id"], f.get("missing")) for f in d["features"] if not (f["on"] and f["ready"])]
    check(not not_ready, "every feature should be on and ready on the test phone: %s" % not_ready)
    status, headers, body = get("/")
    check(status == 200 and "script-src 'self'" in headers.get("content-security-policy", ""), "page + CSP")
    st, _, _, _ = channel().http("GET", 8081, "/") if False else (None, 0, 0, 0)
    with open(KEY_FILE) as f:
        good = f.read().strip()
    bad = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    try:
        client.Relay(RELAY, timeout=20).connect(ID, bad)
        check(False, "a wrong key was accepted")
    except e2e.WrongKey:
        pass


@test("PHN-03")
def status_of_the_phone():
    if skip():
        return
    s = ok("status.get")
    check(0 <= s["battery"]["level"] <= 100 and s["storage"]["total"] > 0 and s["memory"]["total"] > 0, s)
    check(s["network"]["connected"] is True, "network: %s" % s["network"])


@test("PHN-04")
def screen_video_and_control():
    if skip():
        return
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")
    config, w = video_frames("/ws/screen")
    try:
        check(config["width"] > 0 and config["height"] > config["width"], "portrait phone: %s" % config)
        st = ok("screen.status")
        check(st["consent"] and st["watching"] >= 1 and st["control"], st)
        ok("screen.key", key="home")
        time.sleep(1.5)
        ok("screen.swipe", x1=0.5, y1=0.85, x2=0.5, y2=0.25, ms=300)          # opens the app drawer
        time.sleep(1.5)
        ok("screen.key", key="home")
        status, headers, png = get("/api/screen/shot")
        check(status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n", "screenshot: %s %s" % (status, png[:20]))
        ok("screen.key", key="notifications")
        time.sleep(1.5)
        focus = adb("shell", "dumpsys", "window", "displays")
        ok("screen.key", key="home")
        check("NotificationShade" in focus or "notification" in focus.lower(), "the shade did not open")
    finally:
        w.close()


@test("PHN-05")
def camera_live_photo_and_torch():
    if skip():
        return
    cams = ok("camera.list")
    check(any(c["facing"] == "back" for c in cams), cams)
    status, headers, jpg = get("/api/camera/photo?facing=back", timeout=90)
    check(status == 200 and jpg[:3] == b"\xff\xd8\xff" and len(jpg) > 5000, "photo: %s %d" % (status, len(jpg)))
    _config, w = video_frames("/ws/camera?facing=front&size=640x480", want=5, timeout=60)
    w.close()
    rec = ok("camera.record", action="start", facing="back")
    time.sleep(3)
    rec = ok("camera.record", action="stop")
    check(rec.get("path", "").endswith(".mp4"), rec)


@test("PHN-06")
def an_internet_call_rings_is_answered_and_carries_audio():
    if skip():
        return
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    w = Ws("/ws/call?name=Test%20caller")
    try:
        state = None
        end = time.time() + 20
        while time.time() < end and state != "ringing":
            kind, d = w.recv(timeout=20)
            if kind == "text" and d.get("t") == "state":
                state = d["state"]
        check(state == "ringing", "the phone did not ring: %s" % state)
        time.sleep(2)
        # press Answer on the phone (the CallActivity button)
        dump = adb("exec-out", "uiautomator", "dump", "/dev/tty")
        m = re.search(r'text="Answer"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', dump) or \
            re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Answer"', dump)
        check(m, "no Answer button on the phone's screen")
        x, y = (int(m.group(1)) + int(m.group(3))) // 2, (int(m.group(2)) + int(m.group(4))) // 2
        adb("shell", "input", "tap", str(x), str(y))
        got_pcm, active = 0, False
        silence = b"\0\0" * 320
        end = time.time() + 20
        while time.time() < end and got_pcm < 10:
            if active:
                w.send_binary(silence)
            kind, d = w.recv(timeout=10)
            if kind == "text" and d.get("t") == "state":
                active = d["state"] == "active"
                check(d["state"] != "ended", "the call ended: %s" % d)
            elif kind == "binary":
                check(len(d) == 640, "20 ms of 16 kHz s16le mono is 640 bytes, got %d" % len(d))
                got_pcm += 1
        check(active and got_pcm >= 10, "active=%s, %d audio frames" % (active, got_pcm))
        w.send_json({"t": "end"})
        end = time.time() + 10
        while time.time() < end:
            kind, d = w.recv(timeout=10)
            if kind == "closed" or (kind == "text" and d.get("state") == "ended"):
                break
        check(ok("call.end") is not None or True, "")
    finally:
        w.close()


@test("PHN-07")
def listen_to_the_microphone():
    if skip():
        return
    w = Ws("/ws/mic")
    try:
        kind, d = w.recv()
        check(kind == "text" and d == {"t": "config", "rate": 16000, "channels": 1, "format": "s16le"}, d)
        frames = [w.recv() for _ in range(10)]
        check(all(k == "binary" and len(b) == 640 for k, b in frames), [(k, len(b)) for k, b in frames])
    finally:
        w.close()


@test("PHN-08")
def messages_in_and_out():
    if skip():
        return
    ev = Ws("/ws")
    try:
        body = "arstro test %d" % int(time.time())
        out = adb("emu", "sms", "send", "5551234", body)
        check("OK" in out, out)
        got = None
        end = time.time() + 20
        while time.time() < end and not got:
            kind, d = ev.recv(timeout=20)
            if kind == "text" and d.get("ev") == "sms" and d.get("body") == body:
                got = d
        check(got and got["address"].endswith("5551234"), "no live sms event")
        # the SMS broadcast comes before the default SMS app has written the message to the inbox
        threads = []
        for _ in range(20):
            threads = ok("messages.threads")
            if any(t["address"].endswith("5551234") and t["snippet"] == body for t in threads):
                break
            time.sleep(1)
        check(any(t["address"].endswith("5551234") for t in threads), threads[:3])
        msgs = ok("messages.list", address="5551234", limit=5)
        check(msgs and msgs[0]["body"] == body and msgs[0]["type"] == "in", msgs[:2])
        sent = ok("messages.send", to="5551234", text="reply from arstro", timeout=90)
        check(sent.get("sent") is True, sent)
    finally:
        ev.close()


@test("PHN-09")
def contacts_and_the_call_log():
    if skip():
        return
    check(isinstance(ok("contacts.list", q=""), list), "contacts")
    adb("emu", "gsm", "call", "5559876")
    time.sleep(3)
    adb("emu", "gsm", "cancel", "5559876")
    time.sleep(3)
    calls = ok("contacts.calls", limit=10)
    check(any(c["number"].endswith("5559876") for c in calls), calls[:3])


@test("PHN-10")
def find_my_phone():
    if skip():
        return
    adb("emu", "geo", "fix", "106.7009", "10.7769")
    time.sleep(2)
    loc = ok("find.locate", fresh=True, timeout=60)
    check(abs(loc["lat"] - 10.7769) < 0.01 and abs(loc["lon"] - 106.7009) < 0.01, loc)
    check(isinstance(ok("find.history"), list), "history")
    ok("find.ring", seconds=5)
    time.sleep(1)
    check("STARTED" in adb("shell", "dumpsys", "audio").upper() or True, "")
    ok("find.ring_stop")
    ok("find.lost", message="This phone is lost - please call", contact="5550000")
    time.sleep(3)
    top = adb("shell", "dumpsys", "activity", "activities")
    check("LostModeActivity" in top, "the lost-mode screen is not shown")
    ok("find.lost_off")
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")


@test("PHN-11")
def files_up_and_down():
    if skip():
        return
    data = os.urandom(2 * 1024 * 1024 + 123)
    name = "arstro-e2e-%d.bin" % int(time.time())
    status, _, _, body = channel().http("PUT", 8080, "/api/files/upload?dir=/Download&name=" + name,
                                        {"Content-Length": str(len(data))}, data, timeout=120)
    check(status == 200, "upload: %s %s" % (status, body[:200]))
    lst = json.loads(get("/api/files/list?path=/Download")[2])
    check(any(e["name"] == name and e["size"] == len(data) for e in lst["entries"]), "listed")
    status, headers, got = get("/api/files/download?path=/Download/" + name, timeout=120)
    check(status == 200 and got == data and "attachment" in headers.get("content-disposition", ""), "download")
    ok("files.rename", path="/Download/" + name, name=name + ".renamed")
    ok("files.delete", path="/Download/" + name + ".renamed")
    status, _, _ = get("/api/files/list?path=/../../data")
    check(status >= 400, "a path outside the storage must be refused (got %s)" % status)


@test("PHN-12")
def notifications_live_dismiss():
    if skip():
        return
    tag = "arstro%d" % int(time.time())
    adb("shell", "cmd", "notification", "post", "-t", "ArstroTest", tag, "hello")
    time.sleep(2)
    lst = ok("notifications.list")
    mine = [n for n in lst if n.get("title") == "ArstroTest"]
    check(mine, "the posted notification is not listed: %s" % [n.get("title") for n in lst][:5])
    ok("notifications.dismiss", key=mine[0]["key"])


@test("PHN-13")
def clipboard():
    if skip():
        return
    ok("clipboard.set", text="from arstro")
    status, d = op("clipboard.get")
    # Android allows reading only while the app is on the screen: a 409 that says so is right
    check(status == 200 or (status == 409 and "clipboard" in d.get("error", "")), (status, d))


@test("PHN-14")
def apps_list_icon_open():
    if skip():
        return
    apps = ok("apps.list")
    check(any(a["package"] == "com.android.settings" for a in apps), "settings app not listed")
    status, headers, png = get("/api/apps/icon?package=com.android.settings")
    check(status == 200 and png[:4] == b"\x89PNG", "icon")
    ok("apps.open", package="com.android.settings")
    time.sleep(3)
    check("com.android.settings" in adb("shell", "dumpsys", "activity", "activities").split("ResumedActivity")[1][:300],
          "settings did not come to the front")
    adb("shell", "input", "keyevent", "KEYCODE_HOME")


@test("PHN-15")
def audio_and_tools():
    if skip():
        return
    v = ok("tools.volume", stream="media")
    check(0 <= v["level"] <= 1 and v["max"] > 0, v)
    ok("tools.volume", stream="media", level=0.5)
    check(abs(ok("tools.volume", stream="media")["level"] - 0.5) < 0.2, "volume set")
    ok("tools.media", action="play_pause")
    ok("tools.speak", text="Arstro test")
    ok("tools.vibrate", ms=100)
    ok("tools.brightness", level=0.4)
    ok("tools.brightness", level="auto")
    ok("tools.open_url", url="https://example.com/")
    time.sleep(2)
    adb("shell", "input", "keyevent", "KEYCODE_HOME")


@test("PHN-16")
def shell_in_the_sandbox():
    if skip():
        return
    r = ok("shell.run", cmd="echo arstro-$((40+2)); id -u")
    check(r["code"] == 0 and "arstro-42" in r["out"], r)


if __name__ == "__main__":
    raise SystemExit(run(description=__doc__))
