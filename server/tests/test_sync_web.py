#!/usr/bin/env python3
"""Sync, web server and CLI tests (ARC-02/03/04, CON-03/04/05, SEC-03, ADM-*, WIFI-07).

Run on the Pi (the server must be running):
    python3 tests/test_sync_web.py [--rotate-token]

--rotate-token also tests token rotation (web pages must log in again afterwards).
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from harness import check, run, test, wait_until

from arstro_remote.client import Client
from arstro_remote.paths import control_socket_path, load_config
from arstro_remote.web import auth

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..")


class Ctx:
    pass


def setup():
    c = Ctx()
    c.port = int(load_config().get("web_port", 8080))
    c.base = "http://127.0.0.1:%d" % c.port
    c.token = auth.load()
    c.local = Client.unix(control_socket_path())
    c.local.call("hello", app="test-local")
    return c


def teardown(c):
    c.local.close()


def http(c, method, path, body=None, token=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(c.base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def cli(*args, env=None, timeout=60):
    r = subprocess.run([sys.executable, "-m", "arstro_remote", *args], cwd=SERVER, capture_output=True,
                       text=True, timeout=timeout, env=dict(os.environ, **(env or {})))
    return r.returncode, r.stdout, r.stderr


# ------------------------------------------------------------------- tests
@test("CON-03", "SEC-03")
def ping_static_and_token_required(c):
    st, _, body = http(c, "GET", "/api/ping")
    check(st == 200 and json.loads(body)["authorized"] is False, body)
    st, _, body = http(c, "GET", "/api/ping", token=c.token)
    check(json.loads(body)["authorized"] is True, body)
    st, h, body = http(c, "GET", "/")
    check(st == 200 and b"<html" in body.lower() or b"<!doctype" in body.lower(), body[:80])
    for path in ("/api/op/ping", "/ws"):
        st, _, _ = http(c, "POST" if "op" in path else "GET", path, body={} if "op" in path else None)
        check(st == 401, "%s without token -> %s" % (path, st))
    st, _, _ = http(c, "POST", "/api/op/ping", body={}, token="wrong-token-xxxxxxxx")
    check(st == 401, st)


@test("CON-03", "SEC-03")
def login_sets_httponly_cookie(c):
    st, _, _ = http(c, "POST", "/api/login", body={"token": "nope"})
    check(st == 401, st)
    st, h, _ = http(c, "POST", "/api/login", body={"token": c.token})
    cookie = h.get("Set-Cookie", "")
    check(st == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie, cookie)
    value = cookie.split(";", 1)[0]
    st, _, body = http(c, "POST", "/api/op/ping", body={}, headers={"Cookie": value})
    check(st == 200 and json.loads(body)["ok"], body)


@test("ARC-02", "ARC-04")
def rest_runs_the_same_ops(c):
    st, _, body = http(c, "POST", "/api/op/stats.get", body={}, token=c.token)
    d = json.loads(body)
    check(st == 200 and d["ok"] and "cpu" in d["data"], body[:200])
    st, _, body = http(c, "POST", "/api/op/admin.status", body={}, token=c.token)
    check(json.loads(body)["ok"], body[:200])
    st, _, body = http(c, "POST", "/api/op/no.such", body={}, token=c.token)
    check(st == 400 and "unknown op" in json.loads(body)["error"], body)


@test("ARC-02", "CON-04", "TERM-01")
def websocket_session_has_every_op(c):
    w = Client.ws(c.base, c.token)
    try:
        h = w.call("hello", app="arstro-web")
        check(h["controller"] == "web" and h["proto"] == 2 and "state" in h, h.get("controller"))
        check(set(h["state"]) >= {"recorder", "terminals", "controllers"}, list(h["state"]))
        tid = w.call("term.open", cols=80, rows=24, ephemeral=True)["term"]
        w.term_write(tid, b"echo OVER_WS_$((3*3))\n")
        check(w.wait_for(lambda: b"OVER_WS_9" in bytes(w.term_output.get(tid, b"")), 8), "no shell output over WS")
        w.call("term.close", term=tid)
    finally:
        w.close()


@test("CON-05", "ARC-03")
def controllers_see_each_other(c):
    w = Client.ws(c.base, c.token)
    try:
        h = w.call("hello", app="arstro-web")
        sess = h["session"]
        ok = c.local.wait_state("controllers", lambda cs: any(x["session"] == sess and x["controller"] == "web"
                                                                for x in cs), 3)
        check(ok, "local controller did not see the web controller (CON-05)")
    finally:
        w.close()
    ok = c.local.wait_state("controllers", lambda cs: all(x["session"] != sess for x in cs), 3)
    check(ok, "controller list not updated after disconnect")


@test("ARC-03", "REC-04")
def change_on_web_arrives_on_cli_and_back(c):
    w = Client.ws(c.base, c.token)
    try:
        w.call("hello", app="arstro-web")
        orig = c.local.call("recorder.settings.get")["preview"]["quality"]
        other = "low" if orig != "low" else "high"
        t0 = time.monotonic()
        w.call("recorder.settings.set", settings={"preview": {"quality": other}})
        ok = c.local.wait_state("recorder.settings", lambda s: s["preview"]["quality"] == other, 1.0)
        check(ok, "CLI did not get the web's change within 1 s (ARC-03)")
        dt = time.monotonic() - t0
        c.local.call("recorder.settings.set", settings={"preview": {"quality": orig}})
        ok = w.wait_state("recorder.settings", lambda s: s["preview"]["quality"] == orig, 1.0)
        check(ok, "web did not get the CLI's change within 1 s")
        print("      web -> cli in %.0f ms" % (dt * 1000))
    finally:
        w.close()


@test("ADM-01", "ADM-02", "ARC-04", "ARC-03")
def pairing_window_from_web_is_seen_by_all(c):
    before = c.local.call("admin.status")["bluetooth"]
    if not before.get("ready"):
        raise AssertionError("bluetooth not ready: %s" % before)
    w = Client.ws(c.base, c.token)
    try:
        w.call("hello", app="arstro-web")
        w.call("admin.pair", seconds=120)          # admin works from a non-local controller now
        ok = c.local.wait_state("pairing", lambda p: p["pairing_open"] and p["pairing_remaining"] > 100, 3)
        check(ok, "pairing change not pushed")
    finally:
        rem = before.get("pairing_remaining", 0)
        w.call("admin.pair", seconds=rem if before.get("pairing_open") else 0)
        w.close()


@test("WIFI-01", "WIFI-07")
def wifi_state_is_published(c):
    st = c.local.call("wifi.status")
    ok = c.local.wait_state("wifi", lambda d: d.get("device") == st["device"], 3)
    check(ok or c.local.call("state.get", topics=["wifi"])["wifi"]["device"] == st["device"], "no wifi state")


@test("ADM-03", "CON-04")
def web_info_and_cli(c):
    info = c.local.call("web.info")
    check(info["port"] == c.port and info["token"] == c.token and info["urls"], info)
    code, out, err = cli("status")
    check(code == 0 and "Arstro Remote" in out and "Controllers" in out, out + err)
    code, out, err = cli("--json", "rec", "status")
    check(code == 0 and "signal" in json.loads(out), err)
    code, out, err = cli("--json", "gallery", "targets")
    check(code == 0 and any(t["id"] == "h264-vpu" for t in json.loads(out)), err)
    code, out, err = cli("term", "run", "echo CLI_$((5*5)); false")
    check(code == 1 and "CLI_25" in out, (code, out, err))


@test("CON-04", "ARC-04")
def remote_cli_with_url_and_token(c):
    code, out, err = cli("--url", c.base, "--token", c.token, "--json", "rec", "status")
    check(code == 0 and "signal" in json.loads(out), err)
    code, out, err = cli("--url", c.base, "--token", "wrong-token-xxxxxxxx", "status")
    check(code != 0 and "token" in (out + err).lower(), out + err)
    code, out, err = cli("rec", "status", env={"ARSTRO_URL": c.base, "ARSTRO_TOKEN": c.token})
    check(code == 0 and "Signal" in out, err)


@test("SEC-03")
def token_rotation_kicks_web_sessions(c):
    if "--rotate-token" not in sys.argv:
        print("      (skipped: add --rotate-token)")
        return
    w = Client.ws(c.base, c.token)
    w.call("hello", app="arstro-web")
    info = c.local.call("web.rotate_token")
    new = info["token"]
    check(new != c.token and len(new) >= 32, "token not changed")
    check(wait_until(lambda: w.closed, 3), "old web session not closed")
    st, _, _ = http(c, "POST", "/api/op/ping", body={}, token=c.token)
    check(st == 401, "old token still accepted")
    st, _, _ = http(c, "POST", "/api/op/ping", body={}, token=new)
    check(st == 200, "new token rejected")
    check(auth.load() == new, "token file not updated")
    c.token = new


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, __doc__))
