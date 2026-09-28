#!/usr/bin/env python3
"""Tiny adb UI driver used to test the app on a real Android device.

Flutter exposes its widgets to uiautomator, so nodes can be found by their
text / semantics label and tapped.

  ANDROID_SERIAL=<serial> scripts/adb_ui.py shot [name]     screenshot -> ./<name>.png
  scripts/adb_ui.py dump                     list visible nodes (label, centre, class)
  scripts/adb_ui.py tap "<regex>" [n]        tap the n-th node whose label matches
  scripts/adb_ui.py wait "<regex>" [secs]    wait until a node matches (exit 1 on timeout)
  scripts/adb_ui.py xy X Y                   tap coordinates

Typing: adb shell input text 'echo%shello'   (%s = space; quote ; and $ for the device shell)
"""
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

SERIAL = os.environ.get("ANDROID_SERIAL")
OUT = os.environ.get("ADB_UI_OUT", os.getcwd())


def adb(*args, **kw):
    cmd = ["adb"] + (["-s", SERIAL] if SERIAL else []) + list(args)
    return subprocess.run(cmd, capture_output=True, **kw)


def shot(name="screen"):
    path = os.path.join(OUT, name + ".png")
    with open(path, "wb") as f:
        f.write(adb("exec-out", "screencap", "-p").stdout)
    print(path)


def nodes():
    for _ in range(3):
        if "dumped" in adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", text=True).stdout:
            break
        time.sleep(1)
    xml = adb("exec-out", "cat", "/sdcard/ui.xml").stdout.decode("utf-8", "replace")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for n in root.iter("node"):
        label = (n.get("text") or "") + ("" if not n.get("content-desc") else " | " + n.get("content-desc"))
        b = re.findall(r"\d+", n.get("bounds", ""))
        if len(b) == 4:
            x1, y1, x2, y2 = map(int, b)
            out.append((label.strip(), (x1 + x2) // 2, (y1 + y2) // 2, n.get("class") or ""))
    return out


def find(pattern):
    rx = re.compile(pattern, re.I | re.S)
    return [n for n in nodes() if n[0] and rx.search(n[0])]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "shot":
        shot(sys.argv[2] if len(sys.argv) > 2 else "screen")
    elif cmd == "dump":
        for label, x, y, cls in nodes():
            if label:
                print("%-60r (%d,%d) %s" % (label[:60], x, y, cls.split(".")[-1]))
    elif cmd == "tap":
        idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        m = find(sys.argv[2])
        if len(m) <= idx:
            print("NOT FOUND: %s" % sys.argv[2])
            return 1
        label, x, y, _ = m[idx]
        adb("shell", "input", "tap", str(x), str(y))
        print("tapped %r at %d,%d" % (label[:50], x, y))
    elif cmd == "wait":
        end = time.time() + (float(sys.argv[3]) if len(sys.argv) > 3 else 20)
        while time.time() < end:
            m = find(sys.argv[2])
            if m:
                print("found %r" % m[0][0][:80])
                return 0
            time.sleep(1)
        print("TIMEOUT waiting for %s" % sys.argv[2])
        return 1
    elif cmd == "xy":
        adb("shell", "input", "tap", sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
