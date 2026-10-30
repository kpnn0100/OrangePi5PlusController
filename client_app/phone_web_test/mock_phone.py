#!/usr/bin/env python3
"""A fake phone for the phone's web UI (docs/phone/PHONE.md), stdlib only.

Serves client_app/android/app/src/main/assets/phone/ at a NESTED path, like the device manager's viewer
does (B/d/<phone id>/8080/), so relative URLs are proven, and implements the phone's API with fake
data: /api/info, POST /api/op/<op> (every op), /ws (retained topics + events), /ws/screen and
/ws/camera (a real H.264 Annex-B clip made with ffmpeg, split into access units), /ws/mic (a sine
wave), /ws/call (answers at once and echoes the audio), files on a temp dir, photo / shot / icons.

    python3 mock_phone.py [--port 8765] [--prefix /arstro/internal/devices/d/test-phone-ab12cd/8080/]

Test hooks (outside the prefix): GET /mock/log, POST /mock/clear, POST /mock/event {"ev", ...},
POST /mock/features {"<id>": {"on", "ready", "missing"}}, POST /mock/state {"topic", "data"},
POST /mock/call_end (the person on the phone hangs up), POST /mock/foreground {"on": bool},
POST /mock/drop (drops every /ws connection: the UI must reconnect).
Prints "READY <url>" when it listens.
"""

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "android", "app", "src", "main", "assets", "phone"))
MEDIA = os.path.join(HERE, "media")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CSP = "script-src 'self'; object-src 'none'; base-uri 'self'"
FPS = 15
STORAGE = "/storage/emulated/0"

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json", ".jpg": "image/jpeg", ".ico": "image/x-icon"}

FEATURES = [
    ("status", "Phone status"), ("screen", "Screen view & control"), ("camera", "Camera"), ("call", "Internet calls"),
    ("mic", "Listen"), ("messages", "Messages (SMS)"), ("contacts", "Contacts & call log"), ("find", "Find my phone"),
    ("files", "Files"), ("notifications", "Notifications"), ("clipboard", "Clipboard"), ("apps", "Apps"),
    ("tools", "Audio & tools"), ("shell", "Shell"),
]
OP_FEATURE = {"status": "status", "screen": "screen", "camera": "camera", "call": "call", "messages": "messages",
              "contacts": "contacts", "find": "find", "files": "files", "notifications": "notifications",
              "clipboard": "clipboard", "apps": "apps", "tools": "tools", "shell": "shell"}
WS_FEATURE = {"/ws/screen": "screen", "/ws/camera": "camera", "/ws/mic": "mic", "/ws/call": "call"}


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------------- media
def ensure_media():
    os.makedirs(MEDIA, exist_ok=True)
    jobs = {
        "screen.h264": ["-f", "lavfi", "-i", f"testsrc=size=720x1280:rate={FPS}", "-t", "4", "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-profile:v", "baseline", "-x264-params",
                        f"keyint={FPS}:min-keyint={FPS}:scenecut=0:repeat-headers=1", "-bsf:v", "h264_mp4toannexb", "-f", "h264"],
        "camera.h264": ["-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={FPS}", "-t", "4", "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-profile:v", "baseline", "-x264-params",
                        f"keyint={FPS}:min-keyint={FPS}:scenecut=0:repeat-headers=1", "-bsf:v", "h264_mp4toannexb", "-f", "h264"],
        "photo.jpg": ["-f", "lavfi", "-i", "testsrc2=size=1280x720", "-frames:v", "1", "-q:v", "4"],
        "shot.png": ["-f", "lavfi", "-i", "testsrc=size=720x1280", "-frames:v", "1"],
    }
    for name, args in jobs.items():
        out = os.path.join(MEDIA, name)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            continue
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, out], check=True)


def nal_units(data):
    """Annex-B → [(start_with_startcode, end, type)]."""
    out, i, n = [], 0, len(data)
    starts = []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            s = i - 1 if i > 0 and data[i - 1] == 0 else i
            starts.append((s, i + 3))
            i += 3
        else:
            i += 1
    for k, (s, h) in enumerate(starts):
        e = starts[k + 1][0] if k + 1 < len(starts) else n
        out.append((s, e, data[h] & 0x1F, h))
    return out


def access_units(path):
    """Split an Annex-B clip into access units: [(key, bytes)] (AUD NALs dropped, like MediaCodec)."""
    data = open(path, "rb").read()
    aus, cur, has_vcl, key = [], [], False, False
    for s, e, t, h in nal_units(data):
        first_mb0 = t in (1, 5) and (data[h + 1] & 0x80) != 0
        if cur and (t == 9 or (t in (6, 7, 8) and has_vcl) or (first_mb0 and has_vcl)):
            aus.append((key, b"".join(cur)))
            cur, has_vcl, key = [], False, False
        if t == 9:
            continue
        cur.append(b"\x00\x00\x00\x01" + data[h:e])
        if t in (1, 5):
            has_vcl = True
        if t == 5:
            key = True
    if cur:
        aus.append((key, b"".join(cur)))
    return aus


def avc_codec(aus):
    for key, au in aus:
        for s, e, t, h in nal_units(au):
            if t == 7:
                return "avc1.%02x%02x%02x" % (au[h + 1], au[h + 2], au[h + 3])
    return "avc1.42c01f"


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------------- websocket
class WsConn:
    def __init__(self, sock, rfile):
        self.sock = sock
        self.rfile = rfile
        self.lock = threading.Lock()
        self.open = True

    def _frame(self, op, payload):
        n = len(payload)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        return head + payload

    def send(self, data):
        if not self.open:
            return False
        op, payload = (1, data.encode()) if isinstance(data, str) else (2, bytes(data))
        try:
            with self.lock:
                if not self.open:                    # no data after a close frame
                    return False
                self.sock.sendall(self._frame(op, payload))
            return True
        except OSError:
            self.open = False
            return False

    def send_json(self, obj):
        return self.send(json.dumps(obj))

    def close(self, code=1000, reason=""):
        try:
            with self.lock:
                if not self.open:
                    return
                self.open = False
                self.sock.sendall(self._frame(8, struct.pack(">H", code) + reason.encode()[:120]))
        except OSError:
            pass

    def _read(self, n):
        b = self.rfile.read(n)
        if len(b) < n:
            raise EOFError
        return b

    def recv(self):
        """→ str | bytes | None (closed)."""
        parts, first_op = [], None
        try:
            while True:
                b0, b1 = self._read(2)
                fin, op = b0 & 0x80, b0 & 0x0F
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else b"\x00\x00\x00\x00"
                payload = bytearray(self._read(n))
                for i in range(n):
                    payload[i] ^= mask[i % 4]
                if op == 8:
                    try:
                        with self.lock:
                            if self.open:
                                self.open = False
                                self.sock.sendall(self._frame(8, bytes(payload[:2])))
                    except OSError:
                        pass
                    self.open = False
                    return None
                if op == 9:
                    with self.lock:
                        self.sock.sendall(self._frame(10, bytes(payload)))
                    continue
                if op == 10:
                    continue
                if op in (1, 2):
                    first_op = op
                parts.append(bytes(payload))
                if fin:
                    data = b"".join(parts)
                    return data.decode() if first_op == 1 else data
        except (EOFError, OSError, ValueError):
            self.open = False
            return None


# ---------------------------------------------------------------------------------- the fake phone
class Phone:
    def __init__(self):
        self.lock = threading.RLock()
        self.features = {fid: {"id": fid, "title": t, "on": True, "ready": True, "missing": [], "note": None} for fid, t in FEATURES}
        self.topics = {}
        self.clients = set()
        self.log = {}
        self.foreground = False
        self.clipboard = "copied on the phone"
        self.torch = False
        self.call_ws = None
        self.recording = False
        self.volumes = {"media": [9, 15], "ring": [5, 7], "alarm": [6, 7], "call": [3, 5]}
        self.root = tempfile.mkdtemp(prefix="mock-phone-")
        self._seed_files()
        t0 = now_ms()
        self.contacts = [
            {"name": "Alice Nguyen", "numbers": ["+84 90 123 4567"], "emails": ["alice@example.com"]},
            {"name": "Bob Tran", "numbers": ["+84 91 222 3344", "+84 28 3822 1100"]},
            {"name": "Carol Le", "numbers": ["+84 93 555 0101"]},
            {"name": "Dad", "numbers": ["+84 98 765 4321"]},
            {"name": "Pizza Place", "numbers": ["1900 1234"]},
        ]
        self.calls = [
            {"number": "+84901234567", "name": "Alice Nguyen", "type": "in", "date": t0 - 3600_000, "duration_s": 312},
            {"number": "+84912223344", "name": "Bob Tran", "type": "missed", "date": t0 - 7200_000, "duration_s": 0},
            {"number": "+84935550101", "name": "Carol Le", "type": "out", "date": t0 - 86400_000, "duration_s": 75},
            {"number": "+84281234999", "type": "rejected", "date": t0 - 2 * 86400_000, "duration_s": 0},
        ]
        self.messages = {
            1: [{"id": 11, "address": "+84901234567", "name": "Alice Nguyen", "body": "Are we still on for lunch?", "date": t0 - 5400_000, "type": "in", "read": True},
                {"id": 12, "address": "+84901234567", "name": "Alice Nguyen", "body": "Yes! 12:30 at the usual place.", "date": t0 - 5300_000, "type": "out", "read": True},
                {"id": 13, "address": "+84901234567", "name": "Alice Nguyen", "body": "Great, see you there", "date": t0 - 600_000, "type": "in", "read": False}],
            2: [{"id": 21, "address": "+84912223344", "name": "Bob Tran", "body": "Call me when you can", "date": t0 - 2 * 86400_000, "type": "in", "read": True}],
            3: [{"id": 31, "address": "VCB", "body": "Your OTP is 123456. Do not share it.", "date": t0 - 3 * 86400_000, "type": "in", "read": True}],
        }
        self.next_msg = 100
        self.notifications = [
            {"key": "0|com.whatsapp|1|null|10123", "package": "com.whatsapp", "app": "WhatsApp", "title": "Alice", "text": "Running 5 min late",
             "time": t0 - 120_000, "can_reply": True, "actions": ["Reply", "Mark as read"]},
            {"key": "0|com.google.android.gm|7|null|10100", "package": "com.google.android.gm", "app": "Gmail", "title": "Invoice #2041",
             "text": "Your invoice is ready to download.", "time": t0 - 3600_000, "can_reply": False, "actions": ["Archive"]},
            {"key": "0|android|17|null|1000", "package": "android", "app": "Android System", "title": "USB debugging connected",
             "text": "Tap to turn off USB debugging", "time": t0 - 7200_000, "can_reply": False, "actions": []},
        ]
        names = ["Calculator", "Calendar", "Camera", "Chrome", "Clock", "Contacts", "Drive", "Files", "Gmail", "Maps", "Messages",
                 "Phone", "Photos", "Play Store", "Settings", "Spotify", "Telegram", "WhatsApp", "YouTube", "Zalo",
                 "Bluetooth", "Package installer", "System UI"]
        self.apps = [{"package": "com.example." + n.lower().replace(" ", ""), "label": n, "system": i >= 20} for i, n in enumerate(names)]
        base = (10.7769, 106.7009)
        self.history = [{"lat": base[0] + 0.004 * math.sin(i / 6), "lon": base[1] + 0.006 * math.cos(i / 9) + i * 0.0002,
                         "accuracy_m": 15 + (i % 5) * 6, "time": t0 - (96 - i) * 15 * 60_000} for i in range(96)]
        self.topics = {
            "status": self.status(), "features": self.feature_list(),
            "screen": {"consent": False, "watching": 0, "width": 720, "height": 1280, "rotation": 0, "control": True, "waiting": False},
            "call": {"state": "idle", "since": t0}, "find": {"ringing": False, "lost": False},
        }
        threading.Thread(target=self._status_loop, daemon=True).start()

    # -------------------------------------------------------------- helpers
    def _seed_files(self):
        r = self.root
        for d in ["DCIM/Camera", "Download", "Documents", "Music", "Movies/Arstro", "Pictures"]:
            os.makedirs(os.path.join(r, d), exist_ok=True)
        open(os.path.join(r, "Documents", "notes.txt"), "w").write("hello from the phone\n")
        open(os.path.join(r, "Download", "report.pdf"), "wb").write(os.urandom(300_000))
        shutil.copy(os.path.join(MEDIA, "photo.jpg"), os.path.join(r, "DCIM", "Camera", "IMG_20261030_101500.jpg"))

    def note(self, key, item):
        with self.lock:
            self.log.setdefault(key, []).append(item)

    def status(self):
        return {"battery": {"level": 76, "charging": True, "plugged": "usb", "temperature_c": 31.5, "health": "good"},
                "storage": {"total": 128_000_000_000, "free": 41_200_000_000}, "memory": {"total": 8_000_000_000, "available": 3_100_000_000},
                "network": {"type": "wifi", "connected": True, "wifi_ssid": "HomeNet", "ip": "192.0.2.42"},
                "signal": {"type": "lte", "level": 3}, "uptime_s": 123456, "screen_on": True, "locked": False}

    def feature_list(self):
        return [copy.deepcopy(self.features[fid]) for fid, _ in FEATURES]

    def info(self):
        return {"name": "Test Pixel", "model": "Pixel 8", "manufacturer": "Google", "android": "15", "sdk": 35, "app_version": "2.2.0",
                "id": "test-phone-ab12cd", "features": self.feature_list()}

    def publish(self, topic, data):
        with self.lock:
            self.topics[topic] = data
            clients = list(self.clients)
        for ws in clients:
            ws.send_json({"ev": "state", "topic": topic, "data": data})

    def event(self, name, data):
        with self.lock:
            clients = list(self.clients)
        for ws in clients:
            ws.send_json({"ev": name, **data})

    def _status_loop(self):
        while True:
            time.sleep(10)
            self.publish("status", self.status())

    def gate(self, fid):
        """None when usable, else (status, sentence) - PHONE.md's 403 / 409."""
        f = self.features.get(fid)
        if not f:
            return 404, "No such feature"
        if not f["on"]:
            return 403, f"{f['title']} is turned off on the phone"
        if not f["ready"]:
            return 409, f"{f['title']} needs {', '.join(f['missing']) or 'a permission'} - allow it in the app"
        return None

    def vpath(self, p):
        """A phone path → a real path under the temp root (or None outside the storage)."""
        p = p or STORAGE
        if not (p == STORAGE or p.startswith(STORAGE + "/")):
            return None
        real = os.path.normpath(os.path.join(self.root, p[len(STORAGE):].lstrip("/")))
        if not (real == self.root or real.startswith(self.root + os.sep)):
            return None
        return real

    def phone_path(self, real):
        rel = os.path.relpath(real, self.root)
        return STORAGE if rel == "." else STORAGE + "/" + rel.replace(os.sep, "/")

    # ------------------------------------------------------------------ ops
    def op(self, name, a):
        self.note("ops", {"op": name, "args": a})
        fid = OP_FEATURE.get(name.split(".")[0])
        if fid:
            g = self.gate(fid)
            if g:
                raise OpError(g[1], g[0])
        fn = getattr(self, "op_" + name.replace(".", "_"), None)
        if not fn:
            raise OpError("Unknown op " + name, 404)
        return fn(a)

    def op_features_list(self, a):
        return self.feature_list()

    def op_status_get(self, a):
        return self.status()

    def op_screen_status(self, a):
        return self.topics["screen"]

    def op_screen_start(self, a):
        s = dict(self.topics["screen"])
        if s["consent"]:
            return {"consent": True, "waiting": False}
        s["waiting"] = True
        self.publish("screen", s)

        def grant():
            time.sleep(1.2)
            s2 = dict(self.topics["screen"], consent=True, waiting=False)
            self.publish("screen", s2)
        threading.Thread(target=grant, daemon=True).start()
        return {"consent": False, "waiting": True}

    def op_screen_stop(self, a):
        self.publish("screen", dict(self.topics["screen"], consent=False, waiting=False))
        return None

    def _xy(self, a, *keys):
        for k in keys:
            v = a.get(k)
            if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise OpError(f"{k} must be a number from 0 to 1")

    def op_screen_tap(self, a):
        self._xy(a, "x", "y")

    op_screen_long = op_screen_tap

    def op_screen_swipe(self, a):
        self._xy(a, "x1", "y1", "x2", "y2")

    def op_screen_scroll(self, a):
        self._xy(a, "x", "y")

    def op_screen_text(self, a):
        if not isinstance(a.get("text"), str):
            raise OpError("text is required")

    def op_screen_key(self, a):
        if a.get("key") not in ("back", "home", "recents", "notifications", "quick_settings", "lock", "power_dialog", "wake"):
            raise OpError("Unknown key " + str(a.get("key")))

    def op_camera_list(self, a):
        return [{"facing": "back", "id": "0", "sizes": ["1920x1080", "1280x720", "640x480"], "flash": True},
                {"facing": "front", "id": "1", "sizes": ["1280x720", "640x480"], "flash": False}]

    def op_camera_torch(self, a):
        self.torch = bool(a.get("on"))
        return {"on": self.torch}

    def op_camera_record(self, a):
        if a.get("action") == "start":
            self.recording = True
            return {"recording": True}
        if a.get("action") == "stop":
            self.recording = False
            path = STORAGE + "/Movies/Arstro/VID_%s.mp4" % time.strftime("%Y%m%d_%H%M%S")
            open(self.vpath(path), "wb").write(b"\x00" * 1000)
            return {"recording": False, "path": path}
        raise OpError("action must be start or stop")

    def op_call_start(self, a):
        self.publish("call", {"state": "ringing", "since": now_ms()})
        return {"state": "ringing"}

    def op_call_end(self, a):
        ws = self.call_ws
        if ws:
            ws.send_json({"t": "state", "state": "ended", "reason": "Ended"})
            ws.close()
        self.publish("call", {"state": "ended", "reason": "Ended", "since": now_ms()})

    def op_messages_threads(self, a):
        out = []
        for tid, ms in self.messages.items():
            last = max(ms, key=lambda m: m["date"])
            out.append({"thread": tid, "address": last["address"], "name": last.get("name"), "snippet": last["body"], "date": last["date"],
                        "unread": sum(1 for m in ms if not m["read"])})
        return sorted(out, key=lambda t: -t["date"])[: a.get("limit") or 50]

    def _thread_of(self, address):
        digits = "".join(c for c in str(address) if c.isdigit())[-9:]
        for tid, ms in self.messages.items():
            if "".join(c for c in ms[0]["address"] if c.isdigit())[-9:] == digits and digits:
                return tid
        return None

    def op_messages_list(self, a):
        tid = a.get("thread")
        if tid is None and a.get("address"):
            tid = self._thread_of(a["address"])
        ms = self.messages.get(tid, [])
        for m in ms:
            m["read"] = True
        return sorted(ms, key=lambda m: -m["date"])[: a.get("limit") or 100]

    def op_messages_send(self, a):
        to, text = a.get("to"), a.get("text")
        if not to or not text:
            raise OpError("to and text are required")
        tid = self._thread_of(to)
        if tid is None:
            tid = max(self.messages) + 1
            self.messages[tid] = []
        self.next_msg += 1
        self.messages[tid].append({"id": self.next_msg, "address": to, "body": text, "date": now_ms(), "type": "out", "read": True})
        return {"sent": True}

    def op_contacts_list(self, a):
        q = (a.get("q") or "").lower()
        qd = "".join(c for c in q if c.isdigit())
        res = [c for c in self.contacts if not q or q in c["name"].lower()
               or (qd and any(qd in "".join(x for x in n if x.isdigit()) for n in c["numbers"]))]
        return res[: a.get("limit") or 100]

    def op_contacts_calls(self, a):
        return self.calls[: a.get("limit") or 100]

    def op_find_locate(self, a):
        if a.get("fresh"):
            time.sleep(0.6)
        h = self.history[-1]
        return {"lat": h["lat"] + 0.0003, "lon": h["lon"] + 0.0002, "accuracy_m": 12, "time": now_ms() - (0 if a.get("fresh") else 60_000),
                "provider": "fused"}

    def op_find_history(self, a):
        return self.history[-(a.get("limit") or 96):]

    def op_find_ring(self, a):
        self.publish("find", dict(self.topics["find"], ringing=True))

    def op_find_ring_stop(self, a):
        self.publish("find", dict(self.topics["find"], ringing=False))

    def op_find_lost(self, a):
        if not a.get("message"):
            raise OpError("message is required")
        self.publish("find", dict(self.topics["find"], lost=True))

    def op_find_lost_off(self, a):
        self.publish("find", dict(self.topics["find"], lost=False))

    def op_files_delete(self, a):
        p = self.vpath(a.get("path"))
        if not p or p == self.root or not os.path.exists(p):
            raise OpError("No such file", 404)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)

    def op_files_mkdir(self, a):
        p = self.vpath(a.get("path"))
        if not p:
            raise OpError("Not allowed", 403)
        if os.path.exists(p):
            raise OpError("It already exists", 409)
        os.makedirs(p)

    def op_files_rename(self, a):
        p = self.vpath(a.get("path"))
        name = a.get("name") or ""
        if not p or not os.path.exists(p) or p == self.root:
            raise OpError("No such file", 404)
        if "/" in name or name in ("", ".", ".."):
            raise OpError("Not a valid name")
        dst = os.path.join(os.path.dirname(p), name)
        if os.path.exists(dst):
            raise OpError(name + " already exists", 409)
        os.rename(p, dst)
        return {"path": self.phone_path(dst)}

    def op_notifications_list(self, a):
        return self.notifications

    def op_notifications_dismiss(self, a):
        key = a.get("key")
        self.notifications = [n for n in self.notifications if n["key"] != key]
        self.event("notification", {"key": key, "removed": True})

    def op_notifications_reply(self, a):
        if not any(n["key"] == a.get("key") and n["can_reply"] for n in self.notifications):
            raise OpError("This notification can't be replied to")

    def op_clipboard_set(self, a):
        self.clipboard = a.get("text", "")

    def op_clipboard_get(self, a):
        if not self.foreground:
            raise OpError("Android lets the app read the clipboard only while it is open on the phone - open Arstro Remote Client first", 409)
        return {"text": self.clipboard}

    def op_apps_list(self, a):
        return self.apps

    def op_apps_open(self, a):
        if not any(x["package"] == a.get("package") for x in self.apps):
            raise OpError("No such app", 404)

    def op_tools_volume(self, a):
        s = a.get("stream")
        if s not in self.volumes:
            raise OpError("stream must be media, ring, alarm or call")
        if a.get("level") is not None:
            self.volumes[s][0] = round(float(a["level"]) * self.volumes[s][1])
        cur, mx = self.volumes[s]
        return {"stream": s, "level": cur / mx, "max": mx}

    def op_tools_media(self, a):
        if a.get("action") not in ("play_pause", "next", "previous", "stop"):
            raise OpError("Unknown action")

    def op_tools_speak(self, a):
        if not a.get("text"):
            raise OpError("text is required")

    def op_tools_vibrate(self, a):
        return None

    def op_tools_brightness(self, a):
        lv = a.get("level")
        if lv != "auto" and not (isinstance(lv, (int, float)) and 0 <= lv <= 1):
            raise OpError("level must be 0..1 or auto")

    def op_tools_open_url(self, a):
        if not str(a.get("url", "")).startswith(("http://", "https://")):
            raise OpError("Not a URL")

    def op_shell_run(self, a):
        cmd = (a.get("cmd") or "").strip()
        if cmd.startswith("echo "):
            return {"code": 0, "out": cmd[5:] + "\n"}
        if cmd == "id":
            return {"code": 0, "out": "uid=10234(u0_a234) gid=10234(u0_a234) groups=10234(u0_a234),3003(inet)\n"}
        if cmd.startswith("ls"):
            return {"code": 0, "out": "cache\ncode_cache\nfiles\nshared_prefs\n"}
        return {"code": 127, "out": f"/system/bin/sh: {cmd.split()[0] if cmd else ''}: inaccessible or not found\n"}


class OpError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


PHONE = None
PREFIX = "/"
CLIPS = {}


# ---------------------------------------------------------------------------------- streams
def stream_video(ws, kind, query, rotation):
    aus, codec, (w, h) = CLIPS[kind]
    state = {"key": False, "quality": None}
    ws.send_json({"t": "config", "codec": codec, "width": w, "height": h, "rotation": rotation})

    def reader():
        while ws.open:
            m = ws.recv()
            if m is None:
                break
            if isinstance(m, str):
                try:
                    msg = json.loads(m)
                except ValueError:
                    continue
                PHONE.note(kind, msg)
                if msg.get("t") == "keyframe":
                    state["key"] = True
        ws.open = False
    threading.Thread(target=reader, daemon=True).start()
    i, n, t0 = 0, 0, time.monotonic()
    while ws.open:
        if state["key"]:
            state["key"] = False
            i = 0
        key, au = aus[i]
        ok = ws.send(bytes([1 if key else 0]) + struct.pack(">Q", int(n * 1e6 / FPS)) + au)
        if not ok:
            break
        i = (i + 1) % len(aus)
        n += 1
        delay = t0 + n / FPS - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def pcm_frame(phase, freq=440, amp=0.25):
    out = bytearray()
    for k in range(320):
        out += struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * (phase + k) / 16000)))
    return bytes(out)


def stream_mic(ws):
    ws.send_json({"t": "config", "rate": 16000, "channels": 1, "format": "s16le"})
    threading.Thread(target=lambda: [None for _ in iter(ws.recv, None)], daemon=True).start()
    n, t0 = 0, time.monotonic()
    while ws.open:
        if not ws.send(pcm_frame(n * 320)):
            break
        n += 1
        delay = t0 + n * 0.02 - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def run_call(ws):
    PHONE.call_ws = ws
    stats = {"in_frames": 0, "sizes": [], "state": "ringing"}
    with PHONE.lock:
        PHONE.log["call"] = stats
    ws.send_json({"t": "state", "state": "ringing"})
    PHONE.publish("call", {"state": "ringing", "since": now_ms()})

    def answer():
        time.sleep(0.8)
        if ws.open:
            stats["state"] = "active"
            ws.send_json({"t": "state", "state": "active"})
            PHONE.publish("call", {"state": "active", "since": now_ms()})
    threading.Thread(target=answer, daemon=True).start()
    while True:
        m = ws.recv()
        if m is None:
            break
        if isinstance(m, bytes):
            stats["in_frames"] += 1
            if len(stats["sizes"]) < 50:
                stats["sizes"].append(len(m))
            if stats["state"] == "active":
                ws.send(m)                           # echo
        else:
            try:
                msg = json.loads(m)
            except ValueError:
                continue
            PHONE.note("call_msgs", msg)
            if msg.get("t") == "end":
                stats["state"] = "ended"
                ws.close()
                break
    if PHONE.call_ws is ws:
        PHONE.call_ws = None
    PHONE.publish("call", {"state": "ended", "reason": "Ended", "since": now_ms()})


# ---------------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockPhone/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("MOCK_VERBOSE"):
            sys.stderr.write("mock: " + fmt % args + "\n")

    # ----------------------------------------------------------- replies
    def reply(self, status, body=b"", ctype="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def ok(self, data=None):
        self.reply(200, {"ok": True, "data": data})

    def err(self, status, msg):
        self.reply(status, {"ok": False, "error": msg})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def split(self):
        u = urllib.parse.urlsplit(self.path)
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    # ----------------------------------------------------------- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        path, qs = self.split()
        if path.startswith("/mock/"):
            return self.mock(method, path, qs)
        if PREFIX != "/" and path in ("/", PREFIX.rstrip("/")):
            return self.reply(302, b"", "text/plain", [("Location", PREFIX)])
        if not path.startswith(PREFIX):
            return self.err(404, "Not found")
        rel = "/" + path[len(PREFIX):]
        try:
            if rel.startswith("/ws"):
                return self.websocket(rel, qs)
            if method == "GET" and (rel == "/" or rel.startswith("/static/")):
                return self.static(rel)
            if method == "GET" and rel == "/api/info":
                return self.reply(200, PHONE.info())
            if method == "POST" and rel.startswith("/api/op/"):
                return self.do_op(rel[len("/api/op/"):])
            if rel.startswith("/api/files/"):
                return self.files(method, rel, qs)
            if method == "GET" and rel == "/api/camera/photo":
                g = PHONE.gate("camera")
                if g:
                    return self.err(*g)
                PHONE.note("photos", qs)
                return self.reply(200, open(os.path.join(MEDIA, "photo.jpg"), "rb").read(), "image/jpeg")
            if method == "GET" and rel == "/api/screen/shot":
                return self.reply(200, open(os.path.join(MEDIA, "shot.png"), "rb").read(), "image/png")
            if method == "GET" and rel == "/api/apps/icon":
                d = hashlib.md5((qs.get("package") or "").encode()).digest()
                return self.reply(200, png(48, 48, (60 + d[0] % 160, 60 + d[1] % 160, 60 + d[2] % 160)), "image/png")
            return self.err(404, "Not found")
        except OpError as e:
            return self.err(e.status, str(e))

    def static(self, rel):
        f = "index.html" if rel == "/" else rel[1:]
        real = os.path.normpath(os.path.join(ASSETS, f))
        if not real.startswith(ASSETS + os.sep) or not os.path.isfile(real):
            return self.err(404, "Not found")
        ext = os.path.splitext(real)[1]
        headers = [("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff")] if ext == ".html" else [("X-Content-Type-Options", "nosniff")]
        self.reply(200, open(real, "rb").read(), TYPES.get(ext, "application/octet-stream"), headers)

    def do_op(self, name):
        try:
            args = json.loads(self.body() or b"{}")
        except ValueError:
            return self.err(400, "The body is not JSON")
        try:
            self.ok(PHONE.op(name, args if isinstance(args, dict) else {}))
        except OpError as e:
            self.err(e.status, str(e))

    # ------------------------------------------------------------- files
    def files(self, method, rel, qs):
        g = PHONE.gate("files")
        if g:
            return self.err(*g)
        if method == "GET" and rel == "/api/files/list":
            p = PHONE.vpath(qs.get("path"))
            if not p or not os.path.isdir(p):
                return self.err(404, "No such folder")
            entries = []
            for name in os.listdir(p):
                fp = os.path.join(p, name)
                st = os.stat(fp)
                entries.append({"name": name, "path": PHONE.phone_path(fp), "dir": os.path.isdir(fp),
                                "size": 0 if os.path.isdir(fp) else st.st_size, "mtime": int(st.st_mtime * 1000)})
            parent = None if p == PHONE.root else PHONE.phone_path(os.path.dirname(p))
            return self.reply(200, {"path": PHONE.phone_path(p), "parent": parent, "entries": entries})
        if method == "GET" and rel == "/api/files/download":
            p = PHONE.vpath(qs.get("path"))
            if not p or not os.path.isfile(p):
                return self.err(404, "No such file")
            data = open(p, "rb").read()
            name = os.path.basename(p)
            disp = ("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else max(0, len(data) - int(b))
                end = min(len(data) - 1, int(b)) if a and b else len(data) - 1
                if start >= len(data):
                    return self.reply(416, b"", "text/plain", [("Content-Range", f"bytes */{len(data)}")])
                PHONE.note("downloads", {"path": qs.get("path"), "range": rng})
                return self.reply(206, data[start:end + 1], "application/octet-stream",
                                  [disp, ("Content-Range", f"bytes {start}-{end}/{len(data)}"), ("Accept-Ranges", "bytes")])
            PHONE.note("downloads", {"path": qs.get("path")})
            return self.reply(200, data, "application/octet-stream", [disp, ("Accept-Ranges", "bytes")])
        if method == "PUT" and rel == "/api/files/upload":
            d = PHONE.vpath(qs.get("dir"))
            name = qs.get("name") or ""
            if self.headers.get("Content-Length") is None:
                return self.err(411, "Content-Length is required")
            n = int(self.headers["Content-Length"])
            data = self.rfile.read(n) if n else b""
            if not d or not os.path.isdir(d) or not name or "/" in name:
                return self.err(400, "Bad folder or name")
            dst = os.path.join(d, name)
            if os.path.exists(dst) and qs.get("overwrite") != "1":
                return self.err(409, name + " already exists")
            open(dst, "wb").write(data)
            PHONE.note("uploads", {"dir": qs.get("dir"), "name": name, "size": n, "overwrite": qs.get("overwrite")})
            return self.reply(200, {"ok": True, "path": PHONE.phone_path(dst)})
        return self.err(404, "Not found")

    # --------------------------------------------------------- websocket
    def websocket(self, rel, qs):
        if self.headers.get("Upgrade", "").lower() != "websocket":
            return self.err(400, "WebSocket only")
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        ws = WsConn(self.connection, self.rfile)
        PHONE.note("sockets", rel + ("?" + urllib.parse.urlencode(qs) if qs else ""))
        fid = WS_FEATURE.get(rel)
        if fid:
            g = PHONE.gate(fid)
            if g:
                ws.close(4000 + g[0], g[1])
                return
        if rel == "/ws":
            with PHONE.lock:
                PHONE.clients.add(ws)
                topics = dict(PHONE.topics)
            for t, d in topics.items():
                ws.send_json({"ev": "state", "topic": t, "data": d})
            while ws.recv() is not None:
                pass
            with PHONE.lock:
                PHONE.clients.discard(ws)
        elif rel == "/ws/screen":
            if not PHONE.topics["screen"]["consent"]:
                ws.close(4409, "Screen capture is not allowed yet - waiting for consent on the phone")
                return
            PHONE.publish("screen", dict(PHONE.topics["screen"], watching=PHONE.topics["screen"]["watching"] + 1))
            try:
                stream_video(ws, "screen", qs, 0)
            finally:
                PHONE.publish("screen", dict(PHONE.topics["screen"], watching=max(0, PHONE.topics["screen"]["watching"] - 1)))
        elif rel == "/ws/camera":
            stream_video(ws, "camera", qs, 270 if qs.get("facing") == "front" else 90)
        elif rel == "/ws/mic":
            stream_mic(ws)
        elif rel == "/ws/call":
            run_call(ws)
        else:
            ws.close(4404, "No such socket")

    # ------------------------------------------------------------- hooks
    def mock(self, method, path, qs):
        if path == "/mock/log":
            with PHONE.lock:
                return self.reply(200, PHONE.log)
        if method != "POST":
            return self.err(405, "POST")
        try:
            b = json.loads(self.body() or b"{}")
        except ValueError:
            return self.err(400, "not JSON")
        if path == "/mock/clear":
            with PHONE.lock:
                PHONE.log = {}
        elif path == "/mock/event":
            ev = b.pop("ev")
            if ev == "notification" and not b.get("removed"):
                PHONE.notifications.insert(0, {"package": "com.example.app", "time": now_ms(), "can_reply": False, "actions": [], **b})
            if ev == "sms":
                tid = PHONE._thread_of(b.get("address")) or max(PHONE.messages) + 1
                PHONE.messages.setdefault(tid, []).append({"id": 999, "address": b.get("address"), "name": b.get("name"), "body": b.get("body"),
                                                           "date": b.get("date") or now_ms(), "type": "in", "read": False})
            PHONE.event(ev, b)
        elif path == "/mock/features":
            for fid, ch in b.items():
                PHONE.features[fid].update(ch)
            PHONE.publish("features", PHONE.feature_list())
        elif path == "/mock/state":
            PHONE.publish(b["topic"], b["data"])
        elif path == "/mock/call_end":
            ws = PHONE.call_ws
            if ws:
                ws.send_json({"t": "state", "state": "ended", "reason": "The phone hung up"})
                ws.close()
        elif path == "/mock/drop":
            with PHONE.lock:
                clients = list(PHONE.clients)
            for ws in clients:
                ws.close(1001, "going away")
                try:
                    ws.sock.shutdown(2)
                except OSError:
                    pass
        elif path == "/mock/foreground":
            PHONE.foreground = bool(b.get("on"))
        else:
            return self.err(404, "Not found")
        return self.reply(200, {"ok": True})


def main():
    global PHONE, PREFIX
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--prefix", default="/arstro/internal/devices/d/test-phone-ab12cd/8080/")
    args = ap.parse_args()
    PREFIX = "/" + args.prefix.strip("/") + "/" if args.prefix.strip("/") else "/"
    ensure_media()
    for kind, size in (("screen", (720, 1280)), ("camera", (1280, 720))):
        aus = access_units(os.path.join(MEDIA, kind + ".h264"))
        assert aus and aus[0][0], "the clip must start with a key frame"
        CLIPS[kind] = (aus, avc_codec(aus), size)
    PHONE = Phone()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"READY http://{args.host}:{srv.server_address[1]}{PREFIX}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(PHONE.root, ignore_errors=True)


if __name__ == "__main__":
    main()
