#!/usr/bin/env python3
"""The backup terminal (ADM-09, ADM-10), end to end in a throwaway home: `install.sh --backup`
puts a frozen copy of its own, it runs from that folder alone, guards its shells with a password,
keeps them across reconnects, and reaches a (local) device manager as a device of its own.

    python3 tests/test_backup.py

Touches nothing of the machine's own install: HOME and the XDG folders point into a temp folder,
and the installer runs with --no-start (no systemd unit is enabled).
"""

import http.client
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

from harness import check, run, test, wait_until
from instance import free_port

from arstro_remote.relay import client
from arstro_remote.web import ws

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = "/arstro/internal/devices"
PASSWORD = "pw-" + secrets.token_hex(6)


class Ctx:
    pass


def setup():
    c = Ctx()
    c.home = tempfile.mkdtemp(prefix="arstro-backup-test-")
    os.makedirs(os.path.join(c.home, "run"), mode=0o700)
    c.env = dict(os.environ, HOME=c.home, XDG_DATA_HOME=os.path.join(c.home, ".local/share"),
                 XDG_CONFIG_HOME=os.path.join(c.home, ".config"), XDG_STATE_HOME=os.path.join(c.home, ".local/state"),
                 XDG_RUNTIME_DIR=os.path.join(c.home, "run"))
    c.env.pop("PYTHONPATH", None)
    c.port = free_port()
    c.app = os.path.join(c.home, ".local/share/arstro-backup")
    c.cfg = os.path.join(c.home, ".config/arstro-backup")
    c.proc = None
    c.log = open(os.path.join(c.home, "backup.out"), "a")
    # a device manager of its own (relay/arstro_relay), as tests/test_relay.py runs it
    c.join = "join-" + secrets.token_urlsafe(12)
    rport = free_port()
    c.relay_url = "http://127.0.0.1:%d%s" % (rport, BASE)
    env = dict(os.environ, RELAY_HOST="127.0.0.1", RELAY_PORT=str(rport), RELAY_DATA=os.path.join(c.home, "relay"),
               RELAY_JOIN_TOKEN=c.join, RELAY_RATE_LIMIT="1000/10", RELAY_HEARTBEAT="2")
    env.pop("RELAY_PASSWORD", None)
    c.relay = subprocess.Popen([sys.executable, "-m", "arstro_relay"], cwd=os.path.join(ROOT, "relay"), env=env,
                               stdout=c.log, stderr=subprocess.STDOUT)
    return c


def teardown(c):
    stop_backup(c)
    if c.relay.poll() is None:
        c.relay.terminate()
        c.relay.wait(5)
    c.log.close()
    shutil.rmtree(c.home, ignore_errors=True)


def install(c, *args):
    return subprocess.run(["bash", os.path.join(ROOT, "install.sh"), "--backup", "--no-start", "-y", *args],
                          env=c.env, capture_output=True, text=True, timeout=120)


def start_backup(c):
    stop_backup(c)
    env = dict(c.env, PYTHONPATH=c.app)                       # its own folder only
    c.proc = subprocess.Popen([sys.executable, "-m", "arstro_backup", "run"], cwd="/", env=env,
                              stdout=c.log, stderr=subprocess.STDOUT)
    check(wait_until(lambda: req(c, "GET", "/api/ping")[0] == 200, 10), "the backup did not answer on its port")


def stop_backup(c):
    if c.proc and c.proc.poll() is None:
        c.proc.terminate()
        c.proc.wait(10)
    c.proc = None


def req(c, method, path, body=None, headers=None):
    try:
        h = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
        hd = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hd["Content-Type"] = "application/json"
        h.request(method, path, data, hd)
        r = h.getresponse()
        raw = r.read()
        h.close()
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            d = raw
        return r.status, dict(r.getheaders()), d
    except OSError as e:
        return 0, {}, str(e)


def login(c):
    st, hd, _ = req(c, "POST", "/api/login", {"password": PASSWORD})
    check(st == 200, "login %s" % st)
    return hd["Set-Cookie"].split(";")[0]


def read_until(conn, marker, seconds=10):
    got, end = b"", time.monotonic() + seconds
    conn.settimeout(seconds)
    while marker not in got and time.monotonic() < end:
        op, data = conn.recv_message()
        if op == ws.OP_BINARY:
            got += data
    return got


@test("ADM-09")
def the_installer_puts_a_frozen_copy_of_its_own(c):
    r = install(c, "--port", str(c.port), "--password", PASSWORD)
    check(r.returncode == 0, r.stdout + r.stderr)
    for f in ("web.py", "terms.py", "relay.py", "vendor/agent.py", "vendor/e2e.py", "vendor/pipe.py", "vendor/ws.py",
              "static/xterm.js", "static/addon-fit.js", "static/backup.js", "INSTALLED"):
        check(os.path.isfile(os.path.join(c.app, "arstro_backup", f)), "missing %s" % f)
    with open(os.path.join(c.app, "arstro_backup/vendor/agent.py")) as f:
        agent = f.read()
    check("from . import ws as wsmod" in agent and "from .." not in agent, "the agent copy still imports Arstro Remote")
    with open(os.path.join(c.cfg, "config.json")) as f:
        check(json.load(f)["port"] == c.port, "port not saved")
    pw = os.path.join(c.cfg, "password")
    check(stat.S_IMODE(os.stat(pw).st_mode) == 0o600 and open(pw).read().strip() == PASSWORD, "password file")
    with open(os.path.join(c.cfg, "relay.json")) as f:
        check(json.load(f)["enabled"] is False, "a device manager without one given")
    check(os.access(os.path.join(c.home, ".local/bin/arstro-backup"), os.X_OK), "no arstro-backup command")
    stamp = open(os.path.join(c.app, "arstro_backup/INSTALLED")).read()
    time.sleep(1.1)
    r = install(c)
    check(r.returncode == 0 and "stays" in r.stdout, r.stdout + r.stderr)
    check(open(os.path.join(c.app, "arstro_backup/INSTALLED")).read() == stamp, "installing again replaced the code")
    r = install(c, "--force")
    check(r.returncode == 0 and open(os.path.join(c.app, "arstro_backup/INSTALLED")).read() != stamp,
          "--force did not replace the code: " + r.stdout + r.stderr)


@test("ADM-09")
def it_runs_from_its_own_folder_alone(c):
    env = dict(c.env, PYTHONPATH=c.app)
    probe = ("import sys, arstro_backup.main, arstro_backup.web, arstro_backup.relay, arstro_backup.vendor.agent\n"
             "print(sorted({m.split('.')[0] for m in sys.modules if m.startswith('arstro')}))\n"
             "try:\n    import arstro_remote\n    print('arstro_remote importable')\nexcept ImportError:\n    pass\n")
    out = subprocess.run([sys.executable, "-c", probe], cwd="/", env=env, capture_output=True, text=True, timeout=30)
    check(out.returncode == 0 and out.stdout.strip() == "['arstro_backup']", out.stdout + out.stderr)
    start_backup(c)
    st, _, d = req(c, "GET", "/api/ping")
    check(st == 200 and d["server"] == "arstro-backup" and d["version"], d)
    # its command uses its own copy also when started inside a checkout (whose server/ has another one)
    out = subprocess.run([os.path.join(c.home, ".local/bin/arstro-backup"), "status", "--json"],
                         cwd=os.path.join(ROOT, "server"), env=c.env, capture_output=True, text=True, timeout=30)
    check(out.returncode == 0 and json.loads(out.stdout)["running"] is True, out.stdout + out.stderr)


@test("ADM-09", "SEC-03")
def a_password_guards_the_shells(c):
    for method, path in (("GET", "/api/terms"), ("POST", "/api/terms"), ("GET", "/api/me"), ("GET", "/ws/term?id=1")):
        check(req(c, method, path, {} if method == "POST" else None)[0] == 401, "%s %s without the password" % (method, path))
    st, _, d = req(c, "POST", "/api/login", {"password": "wrong"})
    check(st == 401 and "wrong" in d["error"], d)
    st, hd, _ = req(c, "POST", "/api/login", {"password": PASSWORD})
    cookie = hd.get("Set-Cookie", "")
    check(st == 200 and cookie.startswith("arstro_backup=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie, cookie)
    check(req(c, "GET", "/api/me", headers={"Cookie": cookie.split(";")[0]})[0] == 200, "the cookie does not log in")
    check(req(c, "GET", "/api/me", headers={"Authorization": "Bearer " + PASSWORD})[0] == 200, "Bearer does not log in")
    st, _, page = req(c, "GET", "/")
    check(st == 200 and b"Backup terminal" in page, "no page")


@test("ADM-09")
def shells_live_on_and_replay_their_output(c):
    cookie = login(c)
    st, _, d = req(c, "POST", "/api/terms", {"cols": 100, "rows": 30}, {"Cookie": cookie})
    check(st == 200 and d["id"], d)
    tid = d["id"]
    url = "ws://127.0.0.1:%d/ws/term?id=%d&cols=100&rows=30" % (c.port, tid)
    conn = ws.connect(url, {"Cookie": cookie})
    op, hello = conn.recv_message()
    check(op == ws.OP_TEXT and json.loads(hello)["id"] == tid, hello)
    marker = "backup-%s" % secrets.token_hex(4)
    conn.send_binary(("echo %s-$((6*7))\n" % marker).encode())
    got = read_until(conn, (marker + "-42").encode())
    check((marker + "-42").encode() in got, "the shell did not run the command: %r" % got[-300:])
    conn.close()
    time.sleep(0.5)
    terms = req(c, "GET", "/api/terms", headers={"Cookie": cookie})[2]["terms"]
    check(any(t["id"] == tid for t in terms), "the shell died with its viewer")
    conn = ws.connect(url, {"Cookie": cookie})                # back: the recent output again
    conn.recv_message()
    got = read_until(conn, (marker + "-42").encode(), 5)
    check((marker + "-42").encode() in got, "no replay of the shell's output")
    conn.send_text(json.dumps({"t": "resize", "cols": 120, "rows": 40}))
    conn.send_binary(b"stty size; exit 3\n")
    end, ended = time.monotonic() + 10, None
    while time.monotonic() < end and ended is None:
        op, data = conn.recv_message()
        if op == ws.OP_TEXT:
            ended = json.loads(data)
        elif b"40 120" in data:
            got += b"[size ok]"
    conn.close()
    check(b"[size ok]" in got, "resize did not reach the shell")
    check(ended and ended["t"] == "exit" and ended["code"] == 3, ended)
    check(wait_until(lambda: not any(t["id"] == tid for t in req(c, "GET", "/api/terms", headers={"Cookie": cookie})[2]["terms"]), 5),
          "an ended shell is still listed")


@test("ADM-10")
def it_reaches_the_device_manager_as_a_device_of_its_own(c):
    stop_backup(c)
    r = install(c, "--relay", c.relay_url, "--relay-token", c.join)
    check(r.returncode == 0, r.stdout + r.stderr)
    start_backup(c)
    with open(os.path.join(c.cfg, "relay_identity.json")) as f:
        ident = json.load(f)
    check(stat.S_IMODE(os.stat(os.path.join(c.cfg, "relay_identity.json")).st_mode) == 0o600, "identity not private")
    check("-backup-" in ident["id"], ident["id"])
    relay = client.Relay(c.relay_url, timeout=10)

    def listed():
        d = next((d for d in relay.devices() if d["id"] == ident["id"]), None)
        return d if d and d.get("online") else None
    dev = wait_until(listed, 20)
    check(dev, "the backup is not online at the device manager: %s" % relay.devices())
    check("backup terminal" in dev.get("name", "") and dev.get("web") == c.port, dev)
    ch = relay.connect(ident["id"], ident["key"])
    try:
        status, _reason, _hd, body = ch.http("GET", c.port, "/api/ping")
        check(status == 200 and json.loads(body)["server"] == "arstro-backup", (status, body))
        try:
            status = ch.http("GET", 22, "/")[0]
            check(False, "port 22 reached through the backup (%s)" % status)
        except client.RelayError as e:
            check("not allowed" in str(e) or "refused" in str(e), str(e))
    finally:
        ch.close()


@test("ADM-09")
def slots_and_the_backup_never_share_a_port(c):
    r = subprocess.run(["bash", os.path.join(ROOT, "install.sh"), "--slot", "z", "--port", str(c.port), "--no-deps",
                        "--no-perms", "--no-start", "-y"], env=c.env, capture_output=True, text=True, timeout=60)
    check(r.returncode != 0 and "backup terminal" in r.stderr, "a slot took the backup's port: " + r.stdout + r.stderr)
    check(not os.path.exists(os.path.join(c.home, ".local/share/arstro-remote-z")), "the refused slot was installed")
    slot_cfg = os.path.join(c.home, ".config/arstro-remote-y")
    os.makedirs(slot_cfg)
    with open(os.path.join(slot_cfg, "config.json"), "w") as f:
        json.dump({"web_port": 4242}, f)
    r = install(c, "--port", "4242")
    check(r.returncode != 0 and "arstro-remote-y" in r.stderr, "the backup took a slot's port: " + r.stdout + r.stderr)
    shutil.rmtree(slot_cfg)


@test("ADM-09")
def uninstall_removes_it(c):
    stop_backup(c)
    r = subprocess.run(["bash", os.path.join(ROOT, "server/uninstall.sh"), "--backup", "--purge"], env=c.env,
                       capture_output=True, text=True, timeout=60)
    check(r.returncode == 0, r.stdout + r.stderr)
    for p in (c.app, c.cfg, os.path.join(c.home, ".local/bin/arstro-backup")):
        check(not os.path.exists(p), "still there: %s" % p)


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, __doc__))
