#!/usr/bin/env python3
"""Requirement traceability: which tests prove which requirement (docs/requirements.md).

    scripts/req_coverage.py            write docs/traceability.md
    scripts/req_coverage.py --check    also fail (exit 1) if a requirement has no test
    scripts/req_coverage.py --stdout   print instead of writing

Tests name their requirements like this:
    server/tests/*.py        @test("REC-03", "ARC-03")  above the test function
    server/tests/web/*.mjs   test("name", ["REC-02", ...], async () => ...)
    relay/tests/*.py, relay/tests/**/*.mjs   the same two forms (the device manager)
    app/test/*.dart, devices_app/test/*.dart   // covers: UX-03, REC-01   on the line(s) above test()/testWidgets()
    devices_app/android/app/src/test/**/*.kt   // covers: MGR-03          on the line(s) above @Test fun
    scripts/*, *.sh          # covers: SET-01               anywhere in the file (whole file = one check)
Requirements that can only be checked by hand list the procedure in MANUAL below.
"""

import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REQ_FILE = os.path.join(ROOT, "docs", "requirements.md")
OUT = os.path.join(ROOT, "docs", "traceability.md")
ID = r"[A-Z]{2,5}-\d{2}"

MANUAL = {
    "ARC-07": "Reboot the Pi (after `./install.sh --autologin`): the service is up without anybody logging in.",
    "SEC-01": "Pair a new phone outside and inside the pairing window (`server/tests/bt_pairing_security_test.sh`).",
    "SEC-02": "`server/tests/bt_pairing_security_test.sh`: an untrusted device is refused.",
    "CON-01": "Walk out of Bluetooth range and back: the app reconnects and the shell resumes (also `reconnect_live_test.dart`).",
    "PHN-23": "On an Android 12 phone (or the API 31 emulator), open http://<phone>:8080/ from another computer: Camera and Screen show still pictures and the link to https; on https://<phone>:8443/ they are live.",
    "PHN-24": "On an Android 12 phone, Device page: ringer, Do Not Disturb, Bluetooth on / off, an alarm and a timer appear in the clock app, screen timeout, wallpaper, read a sensor.",
    "WIFI-08": "Start the hotspot from the app over Bluetooth, join it from a laptop and open the web address it shows; stop it: the machine is back on its Wi-Fi.",
}


def requirements():
    reqs = []
    for line in open(REQ_FILE, encoding="utf-8"):
        m = re.match(r"\|\s*(%s)\s*\|\s*(.+?)\s*\|\s*$" % ID, line)
        if m:
            text = re.sub(r"\*\*|`", "", m.group(2))
            short = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
            reqs.append((m.group(1), short[:110] + ("…" if len(short) > 110 else ""),
                         "(withdrawn)" in text))
    return reqs


def ids(s):
    return re.findall(ID, s)


def scan():
    found = {}   # req -> [(suite, test)]

    def add(reqs, suite, name):
        for r in reqs:
            found.setdefault(r, []).append((suite, name))

    tests = os.path.join(ROOT, "server", "tests")
    for f in sorted(os.listdir(tests)):
        p = os.path.join(tests, f)
        if f.endswith(".py"):
            src = open(p, encoding="utf-8").read()
            for m in re.finditer(r"@test\(([^)]*)\)\s*\n\s*def (\w+)", src):
                add(ids(m.group(1)), "server/tests/" + f, m.group(2))
        elif f.endswith(".sh"):
            src = open(p, encoding="utf-8").read()
            for m in re.finditer(r"#\s*covers:\s*(.+)", src):
                add(ids(m.group(1)), "server/tests/" + f, "(script)")
    web = os.path.join(tests, "web")
    if os.path.isdir(web):
        for f in sorted(os.listdir(web)):
            if f.endswith((".mjs", ".js")) and f != "package-lock.json":
                src = open(os.path.join(web, f), encoding="utf-8").read()
                for m in re.finditer(r'test\("(\w+)",\s*\[([^\]]*)\]', src):
                    add(ids(m.group(2)), "server/tests/web/" + f, m.group(1))
    for sub in ("relay/tests", "relay/tests/web", "client_app/phone_web_test"):   # device manager, phone web UI
        d = os.path.join(ROOT, sub)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            src = open(os.path.join(d, f), encoding="utf-8").read() if os.path.isfile(os.path.join(d, f)) else ""
            if f.endswith(".py"):
                for m in re.finditer(r"@test\(([^)]*)\)\s*\n\s*def (\w+)", src):
                    add(ids(m.group(1)), sub + "/" + f, m.group(2))
            elif f.endswith((".mjs", ".js")):
                for m in re.finditer(r'test\("([^"]+)",\s*\[([^\]]*)\]', src):
                    add(ids(m.group(2)), sub + "/" + f, m.group(1))
    kt = os.path.join(ROOT, "client_app", "android", "app", "src", "test")   # the phone app's JVM tests
    for dirpath, _, files in sorted(os.walk(kt)) if os.path.isdir(kt) else []:
        for f in sorted(files):
            if not f.endswith(".kt"):
                continue
            src = open(os.path.join(dirpath, f), encoding="utf-8").read()
            for m in re.finditer(r"//\s*covers:\s*(.+)\n\s*@Test\s+fun\s+(\w+)", src):
                add(ids(m.group(1)), os.path.relpath(os.path.join(dirpath, f), ROOT), m.group(2))
    for app in ("app", "devices_app"):                                       # Flutter widget / unit tests
        dart = os.path.join(ROOT, app, "test")
        for f in sorted(os.listdir(dart)) if os.path.isdir(dart) else []:
            if not f.endswith(".dart"):
                continue
            lines = open(os.path.join(dart, f), encoding="utf-8").read().splitlines()
            pending = []
            for line in lines:
                m = re.search(r"//\s*covers:\s*(.+)", line)
                if m:
                    pending += ids(m.group(1))
                    continue
                t = re.search(r"\b(?:testWidgets|test)\(\s*'([^']+)'", line)
                if t and pending:
                    add(pending, app + "/test/" + f, t.group(1))
                    pending = []
    jvm = os.path.join(ROOT, "devices_app", "android", "app", "src", "test")   # Arstro Devices' JVM tests
    for base, _, files in sorted(os.walk(jvm)):
        for f in sorted(files):
            if not f.endswith(".kt"):
                continue
            pending = []
            for line in open(os.path.join(base, f), encoding="utf-8").read().splitlines():
                m = re.search(r"//\s*covers:\s*(.+)", line)
                if m:
                    pending += ids(m.group(1))
                    continue
                t = re.search(r"@Test\s+fun\s+(\w+)", line)
                if t and pending:
                    add(pending, os.path.relpath(os.path.join(base, f), ROOT), t.group(1))
                    pending = []
    scripts = os.path.join(ROOT, "scripts")
    for f in sorted(os.listdir(scripts)):
        p = os.path.join(scripts, f)
        if os.path.isfile(p) and f != os.path.basename(__file__):
            for m in re.finditer(r"#\s*covers:\s*(.+)", open(p, encoding="utf-8", errors="replace").read()):
                add(ids(m.group(1)), "scripts/" + f, "(script)")
    return found


def render(reqs, found):
    known = {r for r, _, _ in reqs}
    live = [r for r in reqs if not r[2]]
    covered = [r for r, _, _ in live if found.get(r)]
    manual_only = [r for r, _, _ in live if not found.get(r) and r in MANUAL]
    missing = [r for r, _, _ in live if not found.get(r) and r not in MANUAL]
    out = ["# Traceability", "",
           "Generated by `scripts/req_coverage.py` from the test sources - do not edit by hand.", "",
           "%d requirements: %d with automated tests, %d checked by hand, %d without any check."
           % (len(live), len(covered), len(manual_only), len(missing)), ""]
    unknown = sorted(set(found) - known)
    if unknown:
        out += ["**Tests name unknown requirements:** " + ", ".join(unknown), ""]
    out += ["| ID | Requirement | Tests |", "|---|---|---|"]
    for r, text, withdrawn in reqs:
        if withdrawn:
            out.append("| %s | %s | (withdrawn) |" % (r, text))
            continue
        cells = ["`%s` %s" % (s, n) for s, n in found.get(r, [])]
        if r in MANUAL:
            cells.append("manual: " + MANUAL[r])
        out.append("| %s | %s | %s |" % (r, text.replace("|", "/"), "<br>".join(cells) or "**none**"))
    return "\n".join(out) + "\n", missing, unknown


def main():
    reqs = requirements()
    text, missing, unknown = render(reqs, scan())
    if "--stdout" in sys.argv:
        sys.stdout.write(text)
    else:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(text)
        print("wrote %s" % os.path.relpath(OUT, ROOT))
    if missing:
        print("no test for: " + ", ".join(missing))
    if unknown:
        print("tests name unknown requirements: " + ", ".join(unknown))
    if "--check" in sys.argv and (missing or unknown):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
