"""NTWB: the protocol definition, the host (Apps module) and the SDKs - against a throwaway
instance with the example app (server/examples/ntwb/hello) installed in its XDG data dir.

    python3 server/tests/test_ntwb.py [-k NAME]
"""

import json
import os
import re
import socket
import stat
import struct
import sys
import time

from harness import check, run, test, wait_until
from instance import Instance

from arstro_remote.ntwb import spec, wire
from arstro_remote.web import ws

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SERVER)
HELLO = os.path.join(SERVER, "examples", "ntwb", "hello")


def install_manifest(data_home, app_id, **over):
    """Install a manifest the way an app's installer does: <data>/ntwb/apps/<id>/ntwb.json,
    pointing at the example app with absolute paths."""
    with open(os.path.join(HELLO, "ntwb.json")) as f:
        m = json.load(f)
    m.update(id=app_id, exec=[os.path.join(HELLO, "hello_app.py")], web=os.path.join(HELLO, "web"),
             api=os.path.join(HELLO, "api.json"), icon=os.path.join(HELLO, "icon.svg"))
    m.update(over)
    d = os.path.join(data_home, "ntwb", "apps", app_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "ntwb.json"), "w") as f:
        json.dump(m, f)
    return d


class Web:
    """A browser-like client on /ws/app/<id>."""

    def __init__(self, t, app_id, session=None):
        base = t.base.replace("http://", "ws://")
        q = "?session=%s" % session if session else ""
        self.conn = ws.connect("%s/ws/app/%s%s" % (base, app_id, q), {"Authorization": "Bearer " + t.token})
        self.conn.sock.settimeout(20)
        self.n = 0
        self.backlog = []          # received but not yet matched by an until()

    def send(self, msg):
        self.conn.send_text(json.dumps(msg))

    def recv(self):
        op, data = self.conn.recv_message()
        if op == ws.OP_TEXT:
            return "json", json.loads(data)
        return "blob", wire.parse_blob(data)

    def until(self, pred, timeout=20):
        for i, item in enumerate(self.backlog):
            if pred(item):
                del self.backlog[i]
                return item, [item]
        end = time.monotonic() + timeout
        seen = []
        while time.monotonic() < end:
            item = self.recv()
            seen.append(item)
            if pred(item):
                return item, seen
            self.backlog.append(item)
        raise AssertionError("not seen within %ss: %s" % (timeout, seen[-5:]))

    def call(self, method, params=None):
        self.n += 1
        cid = "t%d" % self.n
        self.send({"t": "call", "id": cid, "method": method, "params": params or {}})
        (_, msg), _ = self.until(lambda it: it[0] == "json" and it[1].get("t") == "result" and it[1]["id"] == cid)
        return msg

    def close(self):
        self.conn.close()


def jmsg(t):
    return lambda it: it[0] == "json" and it[1].get("t") == t


def counter_after_ready(w, ready):
    """`ready` may come before the app published its first state (it connects, then publishes):
    the value is then in a `state` message right after."""
    if "counter" in ready["state"]:
        return ready["state"]["counter"]
    (_, st), _ = w.until(lambda it: jmsg("state")(it) and it[1]["key"] == "counter")
    return st["data"]


# ------------------------------------------------------------------- the definition
@test("NTWB-01", "NTWB-10")
def generated_reference_is_committed_and_current(_t):
    for name, text in (("api.json", spec.render_json()), ("API.md", spec.render_markdown())):
        path = os.path.join(ROOT, "docs", "ntwb", name)
        check(os.path.exists(path), "%s missing - python3 -m arstro_remote.ntwb.spec --write docs/ntwb" % path)
        check(open(path).read() == text, "docs/ntwb/%s drifted from spec.py - regenerate it" % name)
    narrative = open(os.path.join(ROOT, "docs", "ntwb", "NTWB.md")).read()
    for m in spec.MESSAGES:                                 # the narrative names every message
        check(("`%s`" % m) in narrative, "NTWB.md never mentions `%s`" % m)
    check(spec.VERSION in narrative, "NTWB.md does not state version %s" % spec.VERSION)


@test("NTWB-09", "NTWB-10")
def web_sdk_speaks_the_same_protocol(_t):
    js = open(os.path.join(SERVER, "arstro_remote", "web", "static", "ntwb", "ntwb.js")).read()
    check(re.search(r'const VERSION = "%s"' % re.escape(spec.VERSION), js), "ntwb.js VERSION differs from spec")
    for d in ("c2h", "h2c"):
        want = sorted(n for n, m in spec.MESSAGES.items() if d in m["dir"])
        got = sorted(re.findall(r'"([a-z.]+)"', re.search(r"%s: \[([^\]]*)\]" % d, js).group(1)))
        check(got == want, "ntwb.js %s %s != spec %s" % (d, got, want))


@test("NTWB-06", "NTWB-04")
def messages_are_validated_and_framed(_t):
    ok = {"t": "call", "id": "1", "method": "add", "params": {"n": 1}}
    check(spec.validate(ok, "c2h") == "call", "a good call")
    bad = [({"t": "nope"}, "unknown message type"), ({"t": "call", "id": "1"}, "needs field 'method'"),
           (dict(ok, extra=1), "has no field 'extra'"), (dict(ok, client="c1"), "set by the host"),
           ({"t": "result", "id": "1", "ok": False}, "needs field 'error'"),
           ({"t": "hello", "ntwb": "2.0.0", "app": "x", "version": "1"}, "not compatible"),
           ({"t": "state", "key": "Bad Key", "data": 1}, "state.key must be"),
           ({"t": "welcome", "ntwb": "1.0.0", "session": "s", "host": {}, "clients": []}, "not allowed")]
    for msg, why in bad:
        try:
            spec.validate(msg, "c2h" if msg.get("t") in ("call", "welcome") else "a2h")
        except spec.SpecError as e:
            check(why in str(e), "%s: %s" % (msg, e))
        else:
            raise AssertionError("accepted %s" % msg)
    frames = wire.encode_json(ok) + wire.encode_blob({"stream": "s", "mime": "x/y"}, b"\x00\x01\x02")
    dec = wire.Decoder()
    items = []
    for i in range(len(frames)):                          # byte by byte: partial frames buffer
        items += dec.feed(frames[i:i + 1])
    check(items == [("json", ok), ("blob", ({"stream": "s", "mime": "x/y"}, b"\x00\x01\x02"))], items)
    try:
        wire.Decoder().feed(struct.pack(">BI", 9, 0))
        raise AssertionError("unknown frame type accepted")
    except wire.WireError:
        pass


# ------------------------------------------------------------------- the host
@test("APP-01", "NTWB-02")
def installed_and_broken_apps_are_listed(t):
    apps = {a["id"]: a for a in t.c.call("apps.list")["apps"] if a["id"]}
    check("hello" in apps and apps["hello"]["problem"] is None and apps["hello"]["origin"] == "installed", apps)
    check(apps["broken"]["problem"] and "cannot execute" in apps["broken"]["problem"], apps.get("broken"))
    lst = t.c.call("apps.list")["apps"]
    check(any(a["problem"] and "manifest" in a["problem"] for a in lst if a["id"] is None), "invalid manifest not listed")
    r = t.c.request("apps.launch", app="broken")
    check(r["ok"] is False and "cannot run" in r["error"], r)
    st = os.stat(t.c.call("apps.list")["socket"])
    check(stat.S_IMODE(st.st_mode) == 0o600, "app socket must be 0600")


@test("APP-06", "APP-04", "NTWB-03", "NTWB-07", "NTWB-08", "APP-03")
def a_browser_opens_the_app_and_uses_it(t):
    t.c.call("apps.stop", app="hello")
    w = Web(t, "hello")
    try:
        (_, ready), seen = w.until(jmsg("ready"))
        states = [m["state"] for k, m in seen if k == "json" and m["t"] == "status"]
        check("starting" in states or "running" in states, states)
        check(ready["app"]["id"] == "hello" and counter_after_ready(w, ready) == 0, ready)
        r = w.call("add", {"n": 3})
        check(r["ok"] and r["data"] == 3, r)
        r = w.call("fail")
        check(not r["ok"] and r["error"] == "this method always fails", r)
        r = w.call("nosuch")
        check(not r["ok"] and "no method named nosuch" in r["error"], r)       # API description (NTWB-07)
        r = w.call("picture", {"size": 32})
        check(r["ok"] and r["data"]["bytes"] > 50, r)
        (_, (hdr, data)), _ = w.until(lambda it: it[0] == "blob")
        check(hdr == {"stream": "picture", "mime": "image/png", "meta": {"size": 32}} and data[:4] == b"\x89PNG", hdr)
        env = t.c.call("apps.info", app="hello")
        check(env["state"] == "running" and env["pid"], env)
    finally:
        w.close()


@test("APP-04", "NTWB-08")
def two_clients_share_one_app(t):
    a, b = Web(t, "hello"), Web(t, "hello")
    try:
        (_, ra), _ = a.until(jmsg("ready"))
        (_, rb), _ = b.until(jmsg("ready"))
        check(ra["client"] != rb["client"], "clients need distinct ids")
        check(ra["session"] == rb["session"] == "main", (ra.get("session"), rb.get("session")))
        counter_after_ready(a, ra)
        base = counter_after_ready(b, rb)
        a.send({"t": "call", "id": "same", "method": "add", "params": {"n": 2}})
        b.send({"t": "call", "id": "same", "method": "add", "params": {"n": 5}})   # same call id: no collision
        (_, res_a), _ = a.until(lambda it: jmsg("result")(it) and it[1]["id"] == "same")
        (_, res_b), _ = b.until(lambda it: jmsg("result")(it) and it[1]["id"] == "same")
        check({res_a["data"], res_b["data"]} <= {base + 2, base + 5, base + 7} and max(res_a["data"], res_b["data"]) == base + 7,
              (res_a, res_b))
        (_, st), _ = b.until(lambda it: jmsg("state")(it) and it[1]["data"] == base + 7)
        (_, ev), _ = b.until(lambda it: jmsg("event")(it) and it[1]["name"] == "added")
        check(ev["data"]["n"] in (2, 5), ev)
        c = Web(t, "hello")                                 # a late client is complete at once
        (_, rc), _ = c.until(jmsg("ready"))
        check(rc["state"]["counter"] == base + 7, rc)
        c.close()
        a.send({"t": "call", "id": "x", "method": "picture", "params": {}})
        (_, (hdr, _d)), _ = a.until(lambda it: it[0] == "blob")
        check(hdr["stream"] == "picture", hdr)
    finally:
        a.close()
        b.close()


@test("NTWB-08")
def every_blob_arrives_unless_it_is_live(t):
    w = Web(t, "hello")
    try:
        w.until(jmsg("ready"))
        r = w.call("burst", {"n": 12})
        check(r["ok"] and r["data"]["sent"] == 12, r)
        got = []
        while len(got) < 12:
            (_, (hdr, _d)), _ = w.until(lambda it: it[0] == "blob")
            got.append(hdr["meta"]["i"])
            check("coalesce" not in hdr, hdr)
        check(got == list(range(12)), "thumbnail-like blobs lost or reordered: %s" % got)
        r = w.call("burst", {"n": 3, "coalesce": True})     # may be thinned, the newest always arrives
        seen = []
        while not seen or seen[-1] != 2:
            (_, (hdr, _d)), _ = w.until(lambda it: it[0] == "blob")
            check(hdr.get("coalesce") is True, hdr)
            seen.append(hdr["meta"]["i"])
        check(seen == sorted(seen), seen)
    finally:
        w.close()


def status_with(w, pred):
    (_, st), _ = w.until(lambda it: jmsg("status")(it) and pred(it[1]))
    return st


@test("APP-04", "APP-09", "NTWB-11", "NTWB-12", "NTWB-03")
def each_session_is_its_own_model_and_its_clients_see_each_other(t):
    t.c.call("apps.stop", app="hello")
    a = Web(t, "hello")                                     # main
    b = Web(t, "hello", "new")                              # a second session: another process, another model
    try:
        (_, ra), _ = a.until(jmsg("ready"))
        (_, rb), _ = b.until(jmsg("ready"))
        s2 = rb["session"]
        check(ra["session"] == "main" and s2 not in ("main", "new"), (ra["session"], s2))
        check(b.call("session")["data"] == {"env": s2, "welcome": s2}, "NTWB_SESSION / welcome.session")
        check(a.call("session")["data"] == {"env": "main", "welcome": "main"}, "main's own session")
        base_a = counter_after_ready(a, ra)
        counter_after_ready(b, rb)
        check(b.call("add", {"n": 40})["data"] == 40, "a new session starts from its own model")
        check(a.call("add", {"n": 1})["data"] == base_a + 1, "main is untouched by the other session")
        c = Web(t, "hello", s2)                             # join the second session by id
        try:
            (_, rc), _ = c.until(jmsg("ready"))
            check(rc["session"] == s2 and rc["state"]["counter"] == 40, rc)
            status_with(b, lambda m: m.get("clients") == 2 and m.get("session") == s2)     # b sees c arrive
            c.call("add", {"n": 2})
            (_, st), _ = b.until(lambda it: jmsg("state")(it) and it[1]["data"] == 42)   # ... and c's edit
        finally:
            c.close()
        status_with(b, lambda m: m.get("clients") == 1)                               # ... and c leave
        r = t.c.call("apps.sessions", app="hello")
        by = {x["session"]: x for x in r["sessions"]}
        check(r["single"] is False and set(by) == {"main", s2} and by[s2]["clients"] == 1 and by[s2]["pid"], r)
        check(t.c.call("apps.call", app="hello", session=s2, method="add", params={"n": 8}) == 50, "apps.call session")
        check(t.c.call("apps.state", app="hello", session=s2, key="counter")["data"] == 50, "apps.state session")
        check(t.c.call("apps.state", app="hello", key="counter")["data"] == base_a + 1, "apps.state main")
        lst = {x["id"]: x for x in t.c.call("apps.list")["apps"] if x["id"]}
        check(len(lst["hello"]["sessions"]) == 2 and lst["hello"]["clients"] == 2, lst["hello"])
        e = t.c.request("apps.call", app="hello", session="s99", method="add", params={"n": 1})
        check(e["ok"] is False and "no session s99" in e["error"], e)
        w = Web(t, "hello", "s99")
        (_, err), _ = w.until(jmsg("error"))
        check("no session s99" in err["error"], err)
        w.close()
        w = Web(t, "solo", "new")                           # a single-session app cannot start another
        (_, err), _ = w.until(jmsg("error"))
        check("one session" in err["error"], err)
        w.close()
        extra = []                                          # the cap: SESSIONS_PER_APP at once
        for _ in range(spec.LIMITS["SESSIONS_PER_APP"] - 2):
            extra.append(t.c.call("apps.launch", app="hello", session="new")["session"])
        e = t.c.request("apps.launch", app="hello", session="new")
        check(e["ok"] is False and "at most %d" % spec.LIMITS["SESSIONS_PER_APP"] in e["error"], e)
        for sid in extra:
            t.c.call("apps.stop", app="hello", session=sid)
        check(wait_until(lambda: {x["session"] for x in t.c.call("apps.sessions", app="hello")["sessions"]} == {"main", s2}, 10),
              "stopped sessions without clients are forgotten")
        again = t.c.call("apps.launch", app="hello", session="new")["session"]
        check(again not in [s2] + extra, "a session id is never reused: %s" % again)
        t.c.call("apps.stop", app="hello", session=again)
        code = os.system("cd %s && ARSTRO_SLOT=t XDG_RUNTIME_DIR=%s XDG_CONFIG_HOME=%s %s -m arstro_remote apps sessions hello "
                         "> %s/cli.out 2>&1" % (SERVER, t.dir + "/run", t.dir + "/cfg", sys.executable, t.dir))
        out = open(t.dir + "/cli.out").read()
        check(code == 0 and "main" in out and s2 in out, out)
    finally:
        a.close()
        b.close()
    t.c.call("apps.stop", app="hello", session=s2)


@test("NTWB-06", "APP-07")
def a_client_cannot_break_the_rules(t):
    w = Web(t, "hello")
    try:
        w.until(jmsg("ready"))
        for bad, why in (({"t": "call", "id": "1", "method": "add", "client": "c9"}, "set by the host"),
                         ({"t": "state", "key": "counter", "data": 99}, "not allowed"),
                         ({"t": "hello"}, "not allowed")):
            w.send(bad)
            (_, e), _ = w.until(jmsg("error"))
            check(why in e["error"], e)
        w.conn.send_binary(b"\x00\x02{}")
        (_, e), _ = w.until(jmsg("error"))
        check("blobs" in e["error"], e)
        check(t.c.call("apps.state", app="hello", key="counter")["data"] != 99, "a client changed app state")
    finally:
        w.close()
    import http.client
    hc = http.client.HTTPConnection("127.0.0.1", t.port, timeout=10)
    hc.request("GET", "/apps/hello/", headers={"Authorization": "Bearer wrong"})
    r = hc.getresponse()
    check(r.status == 302 and r.getheader("Location") == "/#/apps?open=hello", "without the password: %s" % r.status)


@test("APP-08", "NTWB-08", "APP-02")
def cli_and_agents_reach_apps_without_a_browser(t):
    before = t.c.call("apps.state", app="hello", key="counter")["data"]
    check(t.c.call("apps.call", app="hello", method="add", params={"n": 10}) == before + 10, "apps.call add")
    check(t.c.call("apps.call", app="hello", method="echo", params={"x": [1, "y"]}) == {"x": [1, "y"]}, "echo")
    r = t.c.request("apps.call", app="hello", method="fail")
    check(r["ok"] is False and r["error"] == "this method always fails", r)
    api = t.c.call("apps.api", app="hello")
    check(set(api["methods"]) == {"add", "echo", "fail", "picture", "burst", "session", "quit"}, api)
    code = os.system("cd %s && ARSTRO_SLOT=t XDG_RUNTIME_DIR=%s XDG_CONFIG_HOME=%s %s -m arstro_remote apps call hello echo "
                     "'{\"a\": 1}' > %s/cli.out 2>&1" % (SERVER, t.dir + "/run", t.dir + "/cfg", sys.executable, t.dir))
    check(code == 0 and '"a": 1' in open(t.dir + "/cli.out").read(), open(t.dir + "/cli.out").read())


@test("APP-03")
def the_web_ui_and_sdk_are_served(t):
    st, body = t.http("GET", "/apps/hello/")
    check(st == 200 and b"/ntwb/ntwb.js" in body, (st, body[:120]))
    st, body = t.http("GET", "/apps/hello/hello.js")
    check(st == 200 and b"NTWB.connect" in body, st)
    st, body = t.http("GET", "/ntwb/ntwb.js")
    check(st == 200 and spec.VERSION.encode() in body, st)
    st, body = t.http("GET", "/apps/hello/api.json")
    check(st == 200 and json.loads(body)["app"] == "hello", body[:100])
    st, body = t.http("GET", "/apps/hello/icon")
    check(st == 200 and body.startswith(b"<svg"), st)
    for bad in ("/apps/hello/../../../etc/passwd", "/apps/hello/%2e%2e/ntwb.json", "/apps/nosuch/"):
        st, _ = t.http("GET", bad)
        check(st == 404, "%s -> %s" % (bad, st))
    import urllib.request
    req = urllib.request.Request(t.base + "/apps/hello/", headers={"Authorization": "Bearer " + t.token})
    with urllib.request.urlopen(req) as r:
        csp = r.headers.get("Content-Security-Policy", "")
        check("frame-ancestors 'self'" in csp and r.headers.get("X-Frame-Options") == "SAMEORIGIN", csp)


@test("NTWB-05", "APP-05", "APP-02")
def lifecycle_stop_exit_and_logs(t):
    t.c.call("apps.launch", app="hello")
    r = t.c.call("apps.stop", app="hello")
    check(r["state"] == "stopped", r)
    t.c.call("apps.launch", app="hello")
    t.c.request("apps.call", app="hello", method="quit")   # the app says bye and exits by itself
    check(wait_until(lambda: t.c.call("apps.info", app="hello")["state"] == "stopped", 10), "no stop after quit")
    lines = t.c.call("apps.log", app="hello", lines=50)["lines"]
    check(any("launched by" in l for l in lines) and any("hello app ready" in l for l in lines), lines[-6:])
    check(any("exited" in l for l in lines), lines[-4:])


@test("NTWB-05")
def an_app_that_never_says_hello_is_stopped(t):
    r = t.c.call("apps.launch", app="mute", wait=False)
    check(r["state"] == "starting", r)
    ok = wait_until(lambda: t.c.call("apps.info", app="mute")["state"] == "failed", spec.TIMINGS["HELLO_TIMEOUT_S"] + 10)
    info = t.c.call("apps.info", app="mute")
    check(ok and "no hello" in (info["detail"] or ""), info)


@test("NTWB-06", "NTWB-02", "APP-07")
def a_raw_app_is_checked(t):
    path = t.c.call("apps.list")["socket"]

    def connect(hello):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(path)
        s.sendall(wire.encode_json(hello))
        return s

    def read(s):
        dec = wire.Decoder()
        while True:
            items = dec.feed(s.recv(65536))
            if items:
                return items[0][1]
    # no token and no `attach` capability: refused
    s = connect({"t": "hello", "ntwb": "1.0.0", "app": "hello", "version": "x"})
    check("launched by the host" in read(s)["error"], "attach without capability")
    s.close()
    s = connect({"t": "hello", "ntwb": "1.0.0", "app": "attachable", "version": "9.9"})
    welcome = read(s)
    check(welcome["t"] == "welcome" and welcome["ntwb"] == spec.VERSION, welcome)
    s.sendall(wire.encode_json({"t": "state", "key": "counter"}))           # data missing
    e = read(s)
    check(e["t"] == "error" and "needs field 'data'" in e["error"], e)
    s.sendall(wire.encode_json({"t": "result", "id": "h999", "ok": True}))  # nobody asked
    e = read(s)
    check(e["t"] == "error" and "unknown call" in e["error"], e)
    s.sendall(wire.encode_json({"t": "state", "key": "counter", "data": 42}))
    check(wait_until(lambda: t.c.call("apps.state", app="attachable").get("state", {}).get("counter") == 42, 5),
          "state from an attached app")
    info = t.c.call("apps.info", app="attachable")
    check(info["state"] == "running" and info["running_version"] == "9.9", info)
    s.close()
    check(wait_until(lambda: t.c.call("apps.info", app="attachable")["state"] == "stopped", 5), "attached app gone")


@test("APP-01", "NTWB-07")
def a_launch_runs_the_app_as_installed_now(t):
    """Reinstalling an app (a new API description, exec or web dir) takes effect with its next
    process, without anybody listing the apps first - Cosmo's install added methods and the host
    refused them as unknown until a rescan."""
    d = os.path.join(t.dir, "data", "ntwb", "apps", "fresh")
    os.makedirs(d, exist_ok=True)
    api_path = os.path.join(d, "api.json")
    api = json.load(open(os.path.join(HELLO, "api.json")))
    api["app"] = "fresh"
    without = dict(api, methods={k: v for k, v in api["methods"].items() if k != "echo"})
    json.dump(without, open(api_path, "w"))
    install_manifest(os.path.join(t.dir, "data"), "fresh", api=api_path)
    t.c.call("apps.list")
    r = t.c.request("apps.call", app="fresh", method="echo", params={"x": 1})
    check(r["ok"] is False and "no method named echo" in r["error"], r)
    t.c.call("apps.stop", app="fresh")
    json.dump(api, open(api_path, "w"))                    # the "reinstall": echo is in the API now
    t.c.call("apps.launch", app="fresh")                   # no apps.list in between
    check(t.c.call("apps.call", app="fresh", method="echo", params={"x": 2}) == {"x": 2}, "new API in force")
    t.c.call("apps.stop", app="fresh")


def setup():
    t = Instance(["system", "apps"], app="test-ntwb")
    data = os.path.join(t.dir, "data")
    install_manifest(data, "hello")
    install_manifest(data, "solo", single=True)
    install_manifest(data, "broken", exec=["/nonexistent/bin/app"])
    install_manifest(data, "mute", exec=["/bin/sleep", "60"], api=None, capabilities=[])
    install_manifest(data, "attachable", capabilities=["attach"])
    d = os.path.join(data, "ntwb", "apps", "garbage")
    os.makedirs(d)
    open(os.path.join(d, "ntwb.json"), "w").write("{not json")
    for app in ("mute",):                                   # api=None must not be written
        p = os.path.join(data, "ntwb", "apps", app, "ntwb.json")
        m = json.load(open(p))
        m.pop("api", None)
        json.dump(m, open(p, "w"))
    t.start()
    return t


if __name__ == "__main__":
    sys.exit(run(setup, lambda t: t.stop(), __doc__))
