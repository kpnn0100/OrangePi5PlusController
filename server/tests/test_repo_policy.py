#!/usr/bin/env python3
"""Repository policy (ARC-05): no environment-specific values in code, tests or docs.

Runs anywhere (no server needed):  python3 server/tests/test_repo_policy.py

Private IP addresses, MAC addresses, Android serials, home directories and user names
must come from arguments, environment variables or local config - never from the repo.
Documentation examples use placeholders (<pi>, 192.0.2.x, 10.0.0.x, 00:11:22:33:44:55).
Extra forbidden words (your own user name, SSID, ...) can be given in ARSTRO_POLICY_WORDS
(comma separated) or in local.env as ARSTRO_POLICY_WORDS=...
"""

import os
import re
import subprocess

from harness import check, run, test

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# fixtures and placeholders that are fine everywhere
ALLOWED = [
    r"10\.0\.0\.\d+",                    # example LAN in fixtures/docs
    r"192\.0\.2\.\d+",                   # RFC 5737 documentation range
    r"127\.0\.0\.1", r"0\.0\.0\.0",
    r"00:11:22:33:44:55", r"AA:BB:CC:DD:EE:\w\w", r"00:00:00:00:00:00",
    r"C0:FF:EE:\w\w:\w\w:\w\w",
    r"00:11:22:AA:BB:CC",                # address-field hint in the app
    r"02:00:00:00:00:\w\w",              # locally administered MACs in test fixtures
]
RULES = [
    ("private IPv4 address", r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ("MAC address", r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b"),
    ("home directory of a real user", r"/home/(?!pi\b|orangepi\b|<)[a-z_][a-z0-9_-]*"),
    ("Android device serial", r"\bATS\d{6,}\b|\b[0-9a-f]{16}\b(?=.*adb)"),
]
SKIP_DIRS = ()
SKIP_EXT = (".png", ".jpg", ".jks", ".apk", ".lock", ".min.js", ".ttf", ".woff2", ".ico")


def tracked_files():
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-co", "--exclude-standard"],
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if not f.startswith(SKIP_DIRS) and not f.endswith(SKIP_EXT)
            and "/vendor/" not in f and "node_modules/" not in f and os.path.isfile(os.path.join(ROOT, f))]


def extra_words():
    words = os.environ.get("ARSTRO_POLICY_WORDS", "")
    env = os.path.join(ROOT, "local.env")
    if not words and os.path.exists(env):
        m = re.search(r"^ARSTRO_POLICY_WORDS=(.*)$", open(env).read(), re.M)
        words = m.group(1).strip().strip("'\"") if m else ""
    return [w.strip() for w in words.split(",") if w.strip()]


@test("ARC-05")
def no_environment_values_in_the_repo():
    allowed = re.compile("|".join(ALLOWED))
    problems = []
    words = extra_words()
    for f in tracked_files():
        try:
            text = open(os.path.join(ROOT, f), encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for what, rx in RULES:
                for m in re.finditer(rx, line):
                    if not allowed.fullmatch(m.group(0)):
                        problems.append("%s:%d %s: %s" % (f, n, what, m.group(0)))
            for w in words:
                if w in line:
                    problems.append("%s:%d forbidden word: %s" % (f, n, w))
    check(not problems, "environment-specific values:\n  " + "\n  ".join(problems[:40]))


@test("ARC-05", "SEC-03")
def no_secrets_committed():
    names = tracked_files()
    bad = [f for f in names if re.search(r"(^|/)(key\.properties|local\.env|web_token)$|\.(jks|keystore)$", f)]
    check(not bad, "secret files are tracked: %s" % bad)


if __name__ == "__main__":
    raise SystemExit(run(description=__doc__))
