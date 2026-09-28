"""`arstro-remote` - the command-line controller (and the server launcher).

On the Pi it talks to the server through the local socket; from any other machine add
--url http://<pi>:8080 --token <token> (or ARSTRO_URL / ARSTRO_TOKEN). Every feature of
the app and the web page is here too (ARC-04), and changes made here show up there live.

    arstro-remote status                 server, Bluetooth, controllers
    arstro-remote watch [TOPIC...]       print live state changes (recorder, jobs, ...)
    arstro-remote rec start|stop|status|settings|set KEY=VALUE|preview FILE
    arstro-remote gallery list|show|convert|delete|delete-take|download|targets
    arstro-remote jobs list|cancel ID|clear
    arstro-remote wifi status|scan|connect SSID|disconnect|forget NAME|radio on|off
    arstro-remote term list|open|attach ID|close ID|run CMD
    arstro-remote input move DX DY|click [BUTTON]|key KEY|type TEXT|scroll DY|pointer
    arstro-remote stats | pair [SECONDS] | unpair ADDRESS | web [--rotate] | call OP [JSON]
    arstro-remote run                    start the server (used by the autostart launcher)
"""

import argparse
import json
import os
import re
import select
import shutil
import signal
import struct
import sys
import time
import urllib.parse
import urllib.request

from . import __version__

APP_ID = "arstro-cli"


# ------------------------------------------------------------------ connection
class Ctl:
    def __init__(self, args):
        self.args = args
        self.url = args.url or os.environ.get("ARSTRO_URL")
        self.token = args.token or os.environ.get("ARSTRO_TOKEN")
        self._client = None
        self.hello = None

    @property
    def client(self):
        if self._client is None:
            from .client import Client
            if self.url:
                if not self.token:
                    sys.exit("arstro-remote: --url needs a token (--token or ARSTRO_TOKEN)")
                try:
                    self._client = Client.ws(self.url, self.token)
                except PermissionError:
                    sys.exit("arstro-remote: the server refused the token")
                except OSError as e:
                    sys.exit("arstro-remote: cannot reach %s (%s)" % (self.url, e))
            else:
                from .paths import control_socket_path
                try:
                    self._client = Client.unix(control_socket_path())
                except OSError as e:
                    sys.exit("arstro-remote: server not reachable at %s (%s)\n"
                             "  (from another machine use --url http://<pi>:8080 --token ...)"
                             % (control_socket_path(), e))
            self.hello = self._client.call("hello", app=APP_ID, version=__version__)
            self._client.state.update(self.hello.get("state") or {})
        return self._client

    def call(self, op, timeout=120, **params):
        try:
            return self.client.call(op, timeout=timeout, **params)
        except RuntimeError as e:
            msg = str(e)
            sys.exit("error: " + (msg.split(" failed: ", 1)[1] if " failed: " in msg else msg))

    # HTTP (downloads, preview) needs a base URL and the token
    def http_base(self):
        if self.url:
            return self.url.rstrip("/"), self.token
        from .paths import load_config
        from .web import auth
        cfg = load_config()
        return "http://127.0.0.1:%d" % int(cfg.get("web_port", 8080)), auth.load()


def out(args, data, human):
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
    else:
        human(data)


def fmt_size(n):
    n = n or 0
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def fmt_dur(s):
    s = int(round(s or 0))
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


# --------------------------------------------------------------------- status
def cmd_status(ctl, a):
    st = ctl.call("admin.status")

    def human(st):
        bt = st["bluetooth"]
        print("Arstro Remote %s on %s  pid %s  up %s" % (st["version"], st.get("hostname", "?"), st["pid"],
                                                          fmt_dur(st["uptime"])))
        if bt.get("ready"):
            rem = bt.get("pairing_remaining", 0)
            pairing = "open (forever)" if rem < 0 else "open (%ds left)" % rem if bt["pairing_open"] else "closed"
            print("Bluetooth  : %s  %s  channel %s  pairing %s" % (bt["alias"], bt["address"], bt["channel"], pairing))
            for d in bt.get("paired_devices", []):
                print("  paired   : %s (%s)%s%s" % (d["name"], d["address"], "" if d["trusted"] else " NOT trusted",
                                                    " connected" if d["connected"] else ""))
        else:
            print("Bluetooth  : not ready %s" % (bt.get("error") or ""))
        web = st.get("web") or {}
        print("Web        : %s" % (", ".join(web.get("urls", [])) if web.get("enabled") else "disabled"))
        print("Recorder   : %s" % ("available" if st.get("recorder", {}).get("enabled") else "disabled"))
        print("Input      : %s (%s)" % (st["input"]["backend"], "ok" if st["input"]["available"] else "unavailable"))
        print("Controllers: %d connected" % len([s for s in st["sessions"] if s.get("client")]))
        for s in st["sessions"]:
            if s.get("client"):
                print("  #%-3d %-4s %-26s %s" % (s["num"], s["controller"], s["peer"][:26], fmt_dur(s["connected_for"])))
    out(a, st, human)


def cmd_watch(ctl, a):
    c = ctl.client
    topics = set(a.topics or [])
    print("watching %s (Ctrl+C to stop)" % (", ".join(sorted(topics)) or "all state topics"), flush=True)

    def show(msg):
        if msg.get("ev") != "state" or (topics and msg.get("topic") not in topics):
            return
        stamp = time.strftime("%H:%M:%S")
        data = msg.get("data")
        if a.json:
            print(json.dumps({"time": stamp, "topic": msg["topic"], "data": data}), flush=True)
            return
        print("%s %-18s %s" % (stamp, msg["topic"], summarize(msg["topic"], data)), flush=True)
    c.on_event = show
    try:
        while not c.closed:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def summarize(topic, d):
    if d is None:
        return "-"
    if topic == "recorder":
        r = d.get("recording") or {}
        sig = d.get("signal") or {}
        rec = ("REC %s %s %s" % (r.get("kind"), fmt_dur(r.get("elapsed")), fmt_size(r.get("size")))
               if r.get("active") else "idle")
        return "%s | signal %s | preview %s viewers | %s free" % (
            rec, sig.get("text") or "none", d.get("preview", {}).get("viewers", 0),
            fmt_size(d.get("disk", {}).get("free")))
    if topic == "jobs":
        return ", ".join("#%s %s %s %d%%" % (j["id"], j["codec"], j["state"], int(j["progress"] * 100))
                         for j in d) or "no jobs"
    if topic == "gallery":
        return "v%s: %s takes, %s files, %s" % (d.get("version"), d.get("takes"), d.get("files"), fmt_size(d.get("size")))
    if topic == "wifi":
        return "%s %s %s" % ("connected to" if d.get("connected") else "not connected", d.get("ssid") or "",
                             d.get("ip") or "")
    if topic == "terminals":
        return ", ".join("#%d(%d viewers)" % (t["term"], t["viewers"]) for t in d) or "no shells"
    if topic == "controllers":
        return ", ".join("%s#%d" % (c["controller"], c["session"]) for c in d) or "none"
    if topic == "pairing":
        return "pairing %s, %d paired" % ("open" if d.get("pairing_open") else "closed", len(d.get("paired_devices", [])))
    return json.dumps(d)[:160]


def cmd_pair(ctl, a):
    params = {} if a.seconds is None else {"seconds": a.seconds}
    st = ctl.call("admin.pair", **params)
    bt = st["bluetooth"]
    print("pairing %s" % ("open for %ds" % bt["pairing_remaining"] if bt.get("pairing_open") and bt["pairing_remaining"] >= 0
                          else "open (forever)" if bt.get("pairing_open") else "closed"))


def cmd_unpair(ctl, a):
    print(json.dumps(ctl.call("admin.unpair", address=a.address)))


def cmd_web(ctl, a):
    info = ctl.call("web.rotate_token" if a.rotate else "web.info")

    def human(i):
        print("URLs : %s" % (", ".join(i.get("urls", [])) or "no network address"))
        print("Token: %s" % i.get("token"))
        if a.rotate:
            print("(new token - other web pages and remote CLIs must log in again)")
    out(a, info, human)


def cmd_stats(ctl, a):
    s = ctl.call("stats.get")

    def human(s):
        net = s["network"]
        print("%s  %s  up %s" % (s["hostname"], s["os"], fmt_dur(s["uptime"])))
        print("IP       : %s (gateway %s)" % (net.get("ip"), net.get("gateway")))
        if s.get("wifi"):
            print("Wi-Fi    : %s %s%%" % (s["wifi"].get("ssid"), s["wifi"].get("signal")))
        c = s["cpu"]
        print("CPU      : %s%%  load %s  %s" % (c["percent"], " ".join(map(str, c["load"])),
                                                " ".join("%d%%" % p for p in c["per_core"])))
        print("Temp     : " + "  ".join("%s %.0f°C" % (k, v) for k, v in s["temps"].items()))
        m = s["memory"]
        print("Memory   : %s / %s (%s%%)" % (fmt_size(m["used"]), fmt_size(m["total"]), m["percent"]))
        for d in s["disks"]:
            print("Disk %-4s: %s free of %s" % (d["mount"], fmt_size(d["free"]), fmt_size(d["total"])))
        print("Top      : " + ", ".join("%s %s%%" % (p["name"], p["cpu"]) for p in s["processes"][:5]))
    out(a, s, human)


# ----------------------------------------------------------------------- wifi
def cmd_wifi(ctl, a):
    sub = a.wifi_cmd or "status"
    if sub == "status":
        out(a, ctl.call("wifi.status"), lambda s: print(
            "%s: %s  %s  signal %s  ip %s  radio %s" % (s["device"], "connected" if s["connected"] else "not connected",
                                                       s.get("ssid") or "-", s.get("signal"), s.get("ip"),
                                                       "on" if s["enabled"] else "off")))
    elif sub == "scan":
        r = ctl.call("wifi.scan", rescan=True)
        out(a, r, lambda r: [print("%s %-32s %3d%%  %-12s %s" % ("*" if n["in_use"] else " ", n["ssid"], n["signal"],
                                                                 n["security"] or "open", "saved" if n["saved"] else ""))
                             for n in r["networks"]])
    elif sub == "saved":
        r = ctl.call("wifi.saved")
        out(a, r, lambda r: [print("%s %-32s %s" % ("*" if n["active"] else " ", n["ssid"], n["uuid"])) for n in r["networks"]])
    elif sub == "connect":
        pw = a.password
        if a.ask:
            import getpass
            pw = getpass.getpass("Password for %s: " % a.ssid)
        r = ctl.call("wifi.connect", ssid=a.ssid, password=pw, hidden=a.hidden)
        print(r["message"])
    elif sub == "disconnect":
        print(ctl.call("wifi.disconnect")["message"])
    elif sub == "forget":
        print(ctl.call("wifi.forget", name=a.name)["message"])
    elif sub == "radio":
        r = ctl.call("wifi.radio", enabled=a.state == "on")
        print("Wi-Fi radio %s" % ("on" if r["enabled"] else "off"))


# ------------------------------------------------------------------ terminals
def cmd_term(ctl, a):
    sub = a.term_cmd or "list"
    if sub == "list":
        r = ctl.call("term.list")["terminals"]
        out(a, r, lambda ts: [print("#%-3d %3dx%-3d %d viewers  opened by %-4s  %s%s" % (
            t["term"], t["cols"], t["rows"], t["viewers"], t["opened_by"], fmt_dur(t["age"]),
            " (ephemeral)" if t["ephemeral"] else "")) for t in ts] or print("no shells"))
    elif sub == "open":
        cols, rows = shutil.get_terminal_size((100, 30))
        tid = ctl.call("term.open", cols=cols, rows=rows, ephemeral=a.ephemeral)["term"]
        if a.detached:
            print(tid)
            return
        interactive(ctl, tid)
    elif sub == "attach":
        interactive(ctl, a.term)
    elif sub == "close":
        ctl.call("term.close", term=a.term)
        print("closed shell #%d" % a.term)
    elif sub == "run":
        run_command(ctl, a.command, a.timeout)


def interactive(ctl, tid):
    """Attach this terminal to shell `tid` (raw mode). Ctrl+] detaches, the shell keeps running."""
    import termios
    import tty
    c = ctl.client
    if not sys.stdin.isatty():
        sys.exit("attach needs a terminal")
    cols, rows = shutil.get_terminal_size((100, 30))
    c.on_term = lambda t, data: t == tid and os.write(1, data)
    exited = []
    c.on_event = lambda m: m.get("ev") == "term.exit" and m.get("term") == tid and exited.append(m)
    c.call("term.attach", term=tid, cols=cols, rows=rows)
    old = termios.tcgetattr(0)

    def winch(*_):
        cc, rr = shutil.get_terminal_size((100, 30))
        try:
            c.notify("term.resize", term=tid, cols=cc, rows=rr)
        except OSError:
            pass
    signal.signal(signal.SIGWINCH, winch)
    sys.stderr.write("[attached to shell #%d - Ctrl+] to detach]\r\n" % tid)
    try:
        tty.setraw(0)
        while not exited and not c.closed:
            r, _, _ = select.select([0], [], [], 0.3)
            if r:
                data = os.read(0, 4096)
                if not data or b"\x1d" in data:
                    break
                c.term_write(tid, data)
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)
    if exited:
        print("\n[shell #%d exited with code %s]" % (tid, exited[0].get("code")))
    else:
        try:
            c.call("term.detach", term=tid)
        except (RuntimeError, OSError, TimeoutError):
            pass
        print("\n[detached from shell #%d - it keeps running]" % tid)


def run_command(ctl, command, timeout):
    """Run one command in a fresh ephemeral shell, print its output, return its exit code.

    The markers are written split (``__AR''S``) so the shell's echo of the command line
    never matches them; only the output of ``echo``/``printf`` does.
    """
    c = ctl.client
    n = int(time.time() * 1000)
    begin, end = ("__ARS_%d_B" % n).encode(), ("__ARS_%d_E" % n).encode()
    tid = c.call("term.open", cols=200, rows=50, ephemeral=True)["term"]
    quiet = ("stty -echo 2>/dev/null; unset HISTFILE; PS1=''; PS2=''; PROMPT_COMMAND=''; "
             "bind 'set enable-bracketed-paste off' 2>/dev/null\n")
    c.term_write(tid, (quiet + "echo __AR''S_%d_B; %s\n"
                       "__rc=$?; printf '\\n__AR''S_%d_E%%s\\n' \"$__rc\"\n" % (n, command, n)).encode())

    def done():
        out = bytes(c.term_output.get(tid, b""))
        return re.search(re.escape(end) + rb"(\d+)\r?\n", out)

    c.wait_for(done, timeout)
    out = bytes(c.term_output.get(tid, b""))
    m = done()
    try:
        c.call("term.close", term=tid)
    except (RuntimeError, OSError, TimeoutError):
        pass
    body = out.split(begin + b"\r\n", 1)[-1].split(begin + b"\n", 1)[-1]
    body = body.split(end, 1)[0].replace(b"\r\n", b"\n")
    if body.endswith(b"\n"):
        body = body[:-1]                    # the newline printf put before the end marker
    sys.stdout.write(body.decode("utf-8", "replace"))
    sys.stdout.flush()
    if not m:
        sys.exit("arstro-remote: timed out after %ss" % timeout)
    sys.exit(int(m.group(1)))


# ---------------------------------------------------------------------- input
def cmd_input(ctl, a):
    sub = a.input_cmd
    if sub == "move":
        ctl.call("in.move_to", x=a.x, y=a.y) if a.absolute else ctl.client.notify("in.move", dx=a.x, dy=a.y)
    elif sub == "click":
        ctl.client.notify("in.btn", b=a.button, a="double" if a.double else "click")
    elif sub in ("down", "up"):
        ctl.client.notify("in.btn", b=a.button, a=sub)
    elif sub == "scroll":
        ctl.client.notify("in.scroll", dx=a.dx, dy=a.dy)
    elif sub == "key":
        ctl.client.notify("in.key", k=a.key, mods=[m for m in (a.mods or "").split(",") if m])
    elif sub == "type":
        ctl.client.notify("in.text", s=a.text)
    elif sub == "pointer":
        p = ctl.call("in.pointer")
        out(a, p, lambda p: print("%d,%d on %dx%d" % (p["x"], p["y"], p["width"], p["height"])))
        return
    ctl.call("ping")      # everything above has been processed when this returns


# ------------------------------------------------------------------- recorder
def cmd_rec(ctl, a):
    sub = a.rec_cmd or "status"
    if sub == "status":
        st = ctl.call("recorder.status")

        def human(st):
            sig, r, d = st["signal"], st["recording"], st["disk"]
            print("Signal   : %s" % (sig.get("text") if sig.get("present") else "none - %s" % (sig.get("why") or "")))
            if r.get("active"):
                print("Recording: %s %s  %s  %s/s  drops %s%s" % (
                    r["kind"], fmt_dur(r["elapsed"]), fmt_size(r["size"]), fmt_size(r["rate"]), r["drops"],
                    "  (stopping)" if r.get("stopping") else ""))
                print("File     : %s" % r["file"])
            else:
                print("Recording: idle · %s" % st["mode_text"])
            p = st["preview"]
            print("Preview  : %d viewer(s) · %s%s" % (p["viewers"], p["quality"],
                                                      " · %sx%s" % (p["stream"]["width"], p["stream"]["height"]) if p.get("stream") else ""))
            print("Storage  : %s free in %s" % (fmt_size(d["free"]), d["path"]))
            caps = st.get("caps") or {}
            print("Encoders : " + ", ".join(k for k in ("vpu_h264", "vpu_h265", "x265", "ffv1", "gpu_ffv1") if caps.get(k)))
            if st.get("last"):
                l = st["last"]
                print("Last     : %s%s" % (l.get("file"), " (%s)" % l["reason"] if l.get("reason") else ""))
        out(a, st, human)
    elif sub == "start":
        r = ctl.call("recorder.start")
        print("● recording %s" % r.get("file"))
        if a.duration:
            try:
                time.sleep(a.duration)
            finally:
                r = ctl.call("recorder.stop")
                print("■ saved %s (%s)" % (r.get("file"), fmt_size(r.get("size"))))
    elif sub == "stop":
        r = ctl.call("recorder.stop")
        print("■ saved %s (%s)%s" % (r.get("file"), fmt_size(r.get("size")),
                                     " - %s" % r["reason"] if r.get("reason") else ""))
    elif sub == "settings":
        out(a, ctl.call("recorder.settings.get"), lambda s: print(json.dumps(s, indent=2)))
    elif sub == "set":
        from .recorder.settings import parse_assignment
        patch = {}
        for item in a.assignments:
            _deep_merge(patch, parse_assignment(item))
        s = ctl.call("recorder.settings.set", settings=patch)
        print("saved: " + ", ".join(a.assignments))
        if not a.json:
            return
        print(json.dumps(s, indent=2))
    elif sub == "edid":
        ctl.call("recorder.edid", value=a.value)
        print("EDID: %s (the source re-reads it)" % a.value)
    elif sub == "quality":
        ctl.call("recorder.preview.quality", quality=a.value)
        print("preview quality: %s" % a.value)
    elif sub == "preview":
        save_preview(ctl, a.file, a.seconds)


def _deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict):
            _deep_merge(a.setdefault(k, {}), v)
        else:
            a[k] = v


def save_preview(ctl, path, seconds):
    """Write `seconds` of the live preview to an .h264 file (play it with ffplay/VLC)."""
    from .web import ws as wsmod
    base, token = ctl.http_base()
    url = base.replace("https://", "wss://") + "/ws/preview"
    conn = wsmod.connect(url, {"Authorization": "Bearer %s" % token})
    frames, keyed, t0, size = 0, False, time.monotonic(), 0
    with open(path, "wb") as f:
        try:
            while time.monotonic() - t0 < seconds:
                op, data = conn.recv_message()
                if op == wsmod.OP_TEXT:
                    msg = json.loads(data)
                    if msg.get("type") == "state" and msg.get("state") == "no-signal":
                        sys.exit("no HDMI signal: %s" % (msg.get("detail") or ""))
                    continue
                _v, flags, _pts = struct.unpack_from(">BBQ", data, 0)
                if not keyed and not flags & 1:
                    continue
                keyed = True
                f.write(data[10:])
                frames += 1
                size += len(data) - 10
        except wsmod.WSClosed:
            pass
    conn.close()
    print("saved %d frames (%s) of live preview to %s" % (frames, fmt_size(size), path))


# -------------------------------------------------------------------- gallery
def cmd_gallery(ctl, a):
    sub = a.gal_cmd or "list"
    if sub == "list":
        r = ctl.call("gallery.list", kind=a.kind)

        def human(r):
            if not r["takes"]:
                print("no recordings in %s" % r["folder"])
            for t in r["takes"]:
                print("%s  %s  %s  %s" % (t["id"], t["title"], fmt_dur(t["duration"]), fmt_size(t["size"])))
                for i in t["items"]:
                    res = "%dx%d@%s" % (i["width"], i["height"], ("%.2f" % i["fps"]).rstrip("0").rstrip(".")) if i["width"] else ""
                    print("    %-6s %-40s %-18s %9s%s" % (i["kind"], i["id"], res, fmt_size(i["size"]),
                                                          "  (%s)" % i["problem"] if i.get("problem") else ""))
        out(a, r, human)
    elif sub == "show":
        out(a, ctl.call("gallery.get", take=a.take), lambda t: print(json.dumps(t, indent=2)))
    elif sub == "targets":
        r = ctl.call("gallery.targets")["targets"]
        out(a, r, lambda ts: [print("%-10s %-24s %s  %s" % (t["id"], t["title"], "yes" if t["available"] else "no ",
                                                              t["description"])) for t in ts])
    elif sub == "convert":
        params = {"file": a.file, "target": a.to, "quality": a.quality, "scale": a.scale, "preset": a.preset}
        if a.bitrate:
            params.update(bitrate=a.bitrate, rc=a.rc)
        job = ctl.call("gallery.convert", **params)
        print("job #%d: %s -> %s" % (job["id"], job["source"], job["output"]))
        if a.wait:
            wait_job(ctl, job["id"])
    elif sub == "delete":
        r = ctl.call("gallery.delete", file=a.file)
        print("deleted " + ", ".join(r["deleted"]))
    elif sub == "delete-take":
        if not a.yes:
            sys.exit("this deletes every file of %s - add --yes" % a.take)
        r = ctl.call("gallery.delete_take", take=a.take)
        print("deleted " + (", ".join(r["deleted"]) or "nothing"))
    elif sub == "download":
        download(ctl, a.file, a.out)


def wait_job(ctl, job_id):
    c = ctl.client
    last = None
    while True:
        jobs = {j["id"]: j for j in (c.state.get("jobs") or ctl.call("jobs.list")["jobs"])}
        j = jobs.get(job_id)
        if j and j["state"] != last or (j and j["state"] == "running"):
            sys.stderr.write("\r%s %3d%% %s fps   " % (j["state"], int(j["progress"] * 100), j["fps"]))
            last = j["state"]
        if j and j["state"] in ("done", "failed", "cancelled"):
            sys.stderr.write("\n")
            if j["state"] != "done":
                sys.exit("job %s: %s" % (j["state"], j.get("error")))
            print("made %s" % j["output"])
            return
        time.sleep(1)


def download(ctl, name, dest):
    base, token = ctl.http_base()
    url = "%s/api/media/%s?download=1" % (base, urllib.parse.quote(name))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    dest = dest or name
    if os.path.isdir(dest):
        dest = os.path.join(dest, name)
    with urllib.request.urlopen(req, timeout=30) as r, open(dest + ".part", "wb") as f:
        total, done, t0 = int(r.headers.get("Content-Length") or 0), 0, time.monotonic()
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if sys.stderr.isatty():
                sys.stderr.write("\r%s / %s  %s/s   " % (fmt_size(done), fmt_size(total),
                                                          fmt_size(done / max(0.1, time.monotonic() - t0))))
    os.replace(dest + ".part", dest)
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    print("saved %s (%s)" % (dest, fmt_size(done)))


def cmd_jobs(ctl, a):
    sub = a.jobs_cmd or "list"
    if sub == "list":
        r = ctl.call("jobs.list")["jobs"]
        out(a, r, lambda js: [print("#%-3d %-9s %-10s %3d%%  %-40s %s" % (
            j["id"], j["state"], j["codec"], int(j["progress"] * 100), j["output"], j.get("error") or ""))
            for j in js] or print("no jobs"))
    elif sub == "cancel":
        ctl.call("jobs.cancel", id=a.id)
        print("cancelling job #%d" % a.id)
    elif sub == "clear":
        ctl.call("jobs.clear")
        print("cleared finished jobs")


def cmd_call(ctl, a):
    resp = ctl.client.request(a.op, **json.loads(a.params))
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


# ---------------------------------------------------------------------- parser
def build_parser():
    ap = argparse.ArgumentParser(prog="arstro-remote", description="Arstro Remote - Orange Pi 5 Plus controller",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--url", help="server URL for remote use, e.g. http://192.0.2.10:8080 (env ARSTRO_URL)")
    ap.add_argument("--token", help="access token for --url (env ARSTRO_TOKEN; `arstro-remote web` shows it)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="start the server")
    run.add_argument("--no-bluetooth", action="store_true", help="no Bluetooth (testing)")
    run.add_argument("--no-web", action="store_true", help="no web server")
    run.add_argument("--no-recorder", action="store_true", help="no HDMI recorder")
    run.add_argument("--simulate", metavar="WxH@FPS", help="recorder test pattern instead of HDMI RX")
    run.add_argument("--web-port", type=int)
    run.add_argument("--debug", action="store_true")

    sub.add_parser("status", help="server, Bluetooth, web, controllers")
    w = sub.add_parser("watch", help="print live state changes")
    w.add_argument("topics", nargs="*", help="recorder, jobs, gallery, wifi, terminals, pairing, controllers ...")
    p = sub.add_parser("pair", help="open the Bluetooth pairing window")
    p.add_argument("seconds", nargs="?", type=int)
    u = sub.add_parser("unpair", help="forget a paired phone")
    u.add_argument("address")
    wb = sub.add_parser("web", help="web access URLs and token")
    wb.add_argument("--rotate", action="store_true", help="make a new token (logs other web users out)")
    sub.add_parser("stats", help="system monitor snapshot")

    wf = sub.add_parser("wifi", help="Wi-Fi")
    ws_ = wf.add_subparsers(dest="wifi_cmd")
    ws_.add_parser("status")
    ws_.add_parser("scan")
    ws_.add_parser("saved")
    c = ws_.add_parser("connect")
    c.add_argument("ssid")
    c.add_argument("--password")
    c.add_argument("--ask", action="store_true", help="ask for the password (not shown)")
    c.add_argument("--hidden", action="store_true")
    ws_.add_parser("disconnect")
    f = ws_.add_parser("forget")
    f.add_argument("name", help="SSID, profile name or UUID")
    r = ws_.add_parser("radio")
    r.add_argument("state", choices=["on", "off"])

    t = sub.add_parser("term", help="shared shells")
    ts = t.add_subparsers(dest="term_cmd")
    ts.add_parser("list")
    o = ts.add_parser("open")
    o.add_argument("--ephemeral", action="store_true", help="close the shell when this command ends")
    o.add_argument("--detached", action="store_true", help="just open it and print its number")
    at = ts.add_parser("attach")
    at.add_argument("term", type=int)
    cl = ts.add_parser("close")
    cl.add_argument("term", type=int)
    rn = ts.add_parser("run", help="run one command and print its output")
    rn.add_argument("command")
    rn.add_argument("--timeout", type=float, default=60)

    i = sub.add_parser("input", help="mouse and keyboard")
    ins = i.add_subparsers(dest="input_cmd")
    mv = ins.add_parser("move")
    mv.add_argument("x", type=float)
    mv.add_argument("y", type=float)
    mv.add_argument("--absolute", action="store_true")
    for name in ("click", "down", "up"):
        b = ins.add_parser(name)
        b.add_argument("button", nargs="?", default="left", choices=["left", "middle", "right"])
        if name == "click":
            b.add_argument("--double", action="store_true")
    sc = ins.add_parser("scroll")
    sc.add_argument("dy", type=float)
    sc.add_argument("dx", type=float, nargs="?", default=0)
    k = ins.add_parser("key")
    k.add_argument("key", help="X key name (Return, Escape, F5, a ...)")
    k.add_argument("--mods", help="e.g. ctrl,alt")
    ty = ins.add_parser("type")
    ty.add_argument("text")
    ins.add_parser("pointer")

    rc = sub.add_parser("rec", help="HDMI RX recorder")
    rs = rc.add_subparsers(dest="rec_cmd")
    rs.add_parser("status")
    st = rs.add_parser("start")
    st.add_argument("--duration", type=float, help="stop after this many seconds")
    rs.add_parser("stop")
    rs.add_parser("settings")
    se = rs.add_parser("set", help="change settings, e.g. mode=raw h265.bitrate=60 raw.ffv1=true")
    se.add_argument("assignments", nargs="+")
    ed = rs.add_parser("edid")
    ed.add_argument("value", choices=["4k60", "4k30", "1080p", "keep"])
    q = rs.add_parser("quality", help="live preview quality")
    q.add_argument("value", choices=["low", "medium", "high"])
    pv = rs.add_parser("preview", help="save the live preview to an .h264 file")
    pv.add_argument("file")
    pv.add_argument("--seconds", type=float, default=5)

    g = sub.add_parser("gallery", help="recordings")
    gs = g.add_subparsers(dest="gal_cmd")
    gl = gs.add_parser("list")
    gl.add_argument("--kind", choices=["RAW", "H.265", "H.264", "FFV1", "VIDEO"])
    sh = gs.add_parser("show")
    sh.add_argument("take")
    gs.add_parser("targets")
    cv = gs.add_parser("convert")
    cv.add_argument("file")
    cv.add_argument("--to", required=True, help="h264-vpu, h265-vpu, h265-x265, ffv1, ffv1-gpu")
    cv.add_argument("--quality", choices=["high", "higher", "max"], default="high")
    cv.add_argument("--bitrate", type=float, help="Mbit/s instead of constant quality (H.264/H.265)")
    cv.add_argument("--rc", choices=["vbr", "cbr"], default="vbr")
    cv.add_argument("--scale", choices=["source", "1080", "720"], default="source", help="H.264 only")
    cv.add_argument("--preset", default="fast", help="x265 speed preset")
    cv.add_argument("--wait", action="store_true", help="wait and show progress")
    dl = gs.add_parser("delete", help="delete one file (one format of a take)")
    dl.add_argument("file")
    dt = gs.add_parser("delete-take", help="delete every file of a take")
    dt.add_argument("take")
    dt.add_argument("--yes", action="store_true")
    dw = gs.add_parser("download")
    dw.add_argument("file")
    dw.add_argument("--out", help="file or folder")

    j = sub.add_parser("jobs", help="background conversions")
    js = j.add_subparsers(dest="jobs_cmd")
    js.add_parser("list")
    jc = js.add_parser("cancel")
    jc.add_argument("id", type=int)
    js.add_parser("clear")

    ca = sub.add_parser("call", help="send any protocol op")
    ca.add_argument("op")
    ca.add_argument("params", nargs="?", default="{}")
    sub.add_parser("version")
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.cmd == "run":
        from .daemon import Daemon, load_config, setup_logging
        setup_logging(a.debug)
        cfg = load_config()
        if a.no_web:
            cfg["web_enabled"] = False
        if a.no_recorder:
            cfg["recorder_enabled"] = False
        if a.simulate:
            cfg["recorder_simulate"] = a.simulate
        if a.web_port:
            cfg["web_port"] = a.web_port
        Daemon(cfg, use_bluetooth=not a.no_bluetooth).run()
        return 0
    if a.cmd == "version" or a.cmd is None:
        if a.cmd is None:
            ap.print_help()
        else:
            print(__version__)
        return 0
    ctl = Ctl(a)
    handlers = {"status": cmd_status, "watch": cmd_watch, "pair": cmd_pair, "unpair": cmd_unpair,
                "web": cmd_web, "stats": cmd_stats, "wifi": cmd_wifi, "term": cmd_term, "input": cmd_input,
                "rec": cmd_rec, "gallery": cmd_gallery, "jobs": cmd_jobs, "call": cmd_call}
    try:
        return handlers[a.cmd](ctl, a) or 0
    except KeyboardInterrupt:
        return 130
