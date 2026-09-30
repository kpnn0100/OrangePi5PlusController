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
    arstro-remote camera sources|select SOURCE
    arstro-remote net status|devices|connections|show UUID|set UUID ...|up|down|delete UUID
    arstro-remote bt status|power on|off|scan|devices|pair|connect|disconnect|trust|remove ADDR
    arstro-remote io info|header|gpio ...|i2c ...|spi ...|uart ...|pwm ...|led ...|adc
    arstro-remote files ls [PATH]|get PATH|put FILE DIR|rm PATH|mkdir PATH|mv PATH NEW
    arstro-remote system info|modules [--set a,b]|restart|reboot|poweroff
    arstro-remote apps list|info|api|launch|stop|call ID METHOD [JSON]|state ID [KEY]|log ID|register PATH|unregister PATH|spec
    arstro-remote slots [--current|--idle]   which A/B slot hosts this session, which one to deploy into
    arstro-remote log [--file NAME] [-n N] [--grep TEXT] [--follow] | log level LEVEL
    arstro-remote stats | pair [SECONDS] | unpair ADDRESS | web [--rotate] | call OP [JSON]
    arstro-remote run                    start the server (used by the autostart launcher)

--slot a|b (or ARSTRO_SLOT) picks the installed instance on this machine (A/B slots).
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
import urllib.error
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
                try:
                    self._client = Client.ws(self.url, self.token)
                except PermissionError:
                    sys.exit("arstro-remote: the server refused the password (--token or ARSTRO_TOKEN)"
                             if self.token else "arstro-remote: the server needs the password (--token or ARSTRO_TOKEN)")
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
    if a.set_password is not None:
        pw = a.set_password
        if pw == "-":
            pw = sys.stdin.readline().rstrip("\n")
        elif pw == "":
            import getpass
            pw = getpass.getpass("New password: ")
            if getpass.getpass("Again: ") != pw:
                sys.exit("arstro-remote: the passwords differ")
        info = ctl.call("web.set_password", password=pw)
        note = "password changed - other browsers and remote CLIs must log in again"
    elif a.rotate:
        info = ctl.call("web.rotate_token")
        note = "new random password - other browsers and remote CLIs must log in again"
    elif a.open or a.require_password:
        info = ctl.call("web.set_auth", required=bool(a.require_password))
        note = ("anyone on the network can use the web UI now (no password)" if a.open
                else "the web UI and remote CLIs need the password again")
    else:
        info, note = ctl.call("web.info"), None

    def human(i):
        print("URLs    : %s" % (", ".join(i.get("urls", [])) or "no network address"))
        print("Access  : %s" % ("OPEN (no password)" if i.get("auth") == "open" else "password"))
        if i.get("app") and i.get("urls"):
            print("App     : %s (v%s) - open it on the phone to install" % (
                i["urls"][0].rstrip("/") + i["app"]["path"], i["app"].get("version") or "?"))
        if a.show or a.set_password is None:
            print("Password: %s" % (i.get("token") if a.show else "(hidden - add --show)"))
        if note:
            print("(%s)" % note)
    out(a, info, human)


def cmd_stats(ctl, a):
    if getattr(a, "watch", False):              # STAT-02: live, every 2 s
        try:
            while True:
                s = ctl.call("stats.get")
                c, m = s["cpu"], s["memory"]
                print("%s  CPU %5.1f%%  %s°C  mem %s/%s  load %s" % (
                    time.strftime("%H:%M:%S"), c["percent"], "%.0f" % (s.get("cpu_temp") or 0),
                    fmt_size(m["used"]), fmt_size(m["total"]), " ".join(map(str, c["load"]))), flush=True)
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
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
    elif sub == "source":
        st = ctl.call("recorder.source", simulate=None if a.hdmi else a.test, timeout=60) \
            if (a.hdmi or a.test) else ctl.call("recorder.status")
        print("Source: %s" % ("test pattern " + st["simulate"] if st.get("simulate") else "HDMI RX"))


def _deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict):
            _deep_merge(a.setdefault(k, {}), v)
        else:
            a[k] = v


def save_preview(ctl, path, seconds, stream="/ws/preview"):
    """Write `seconds` of a live stream (HDMI preview or /ws/screen) to an .h264 file."""
    from .web import ws as wsmod
    base, token = ctl.http_base()
    url = base.replace("https://", "wss://") + stream
    conn = wsmod.connect(url, {"Authorization": "Bearer %s" % token} if token else {})
    frames, keyed, t0, size = 0, False, time.monotonic(), 0
    with open(path, "wb") as f:
        try:
            while time.monotonic() - t0 < seconds:
                op, data = conn.recv_message()
                if op == wsmod.OP_TEXT:
                    msg = json.loads(data)
                    if msg.get("type") == "state" and msg.get("state") == "no-signal":
                        sys.exit("%s: %s" % ("no HDMI signal" if stream == "/ws/preview" else "no picture",
                                             msg.get("detail") or ""))
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
    print("saved %d frames (%s) of the live %s to %s" % (
        frames, fmt_size(size), "preview" if stream == "/ws/preview" else "screen", path))


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
                    print("    %-6s %-40s %-18s %9s%s%s" % (i["kind"], i["id"], res, fmt_size(i["size"]),
                                                            "  verified lossless" if i.get("verified") else "",
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
        if a.keep_raw:
            params["replace_raw"] = False
        job = ctl.call("gallery.convert", **params)
        print("job #%d: %s -> %s" % (job["id"], job["source"], job["output"]))
        if a.wait:
            wait_job(ctl, job["id"])
    elif sub == "verify":
        job = ctl.call("gallery.verify", file=a.file, delete_raw=a.delete_raw)
        print("job #%d: checking %s against %s byte for byte%s" % (
            job["id"], job["output"], job["source"], ", then deleting the RAW" if a.delete_raw else ""))
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
        if j and j["state"] != last or (j and j["state"] in ("running", "verifying")):
            sys.stderr.write("\r%s %3d%% %s fps   " % (j["state"], int(j["progress"] * 100), j["fps"]))
            last = j["state"]
        if j and j["state"] in ("done", "failed", "cancelled"):
            sys.stderr.write("\n")
            v = j.get("verify")
            if v:
                print(("verified: %s%s" % (v.get("detail"), " - RAW deleted" if v.get("raw_deleted") else ""))
                      if v.get("ok") else "NOT identical: %s - the RAW is kept" % v.get("detail"))
            if j["state"] != "done":
                sys.exit("job %s: %s" % (j["state"], j.get("error") or (v or {}).get("detail")))
            if j["codec"] != "verify":
                print("made %s" % j["output"])
            if v and not v.get("ok"):
                sys.exit(1)
            return
        time.sleep(1)


def download(ctl, name, dest):
    base, token = ctl.http_base()
    url = "%s/api/media/%s?download=1" % (base, urllib.parse.quote(name))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token} if token else {})
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
        def verdict(j):
            v = j.get("verify")
            if not v:
                return j.get("error") or ""
            return ("verified%s" % (", RAW deleted" if v.get("raw_deleted") else "")) if v.get("ok") \
                else "NOT identical: " + str(v.get("detail"))
        out(a, r, lambda js: [print("#%-3d %-9s %-10s %3d%%  %-40s %s" % (
            j["id"], j["state"], j["codec"], int(j["progress"] * 100), j["output"], verdict(j)))
            for j in js] or print("no jobs"))
    elif sub == "cancel":
        ctl.call("jobs.cancel", id=a.id)
        print("cancelling job #%d" % a.id)
    elif sub == "clear":
        ctl.call("jobs.clear")
        print("cleared finished jobs")


def cmd_screen(ctl, a):
    sub = a.screen_cmd or "status"
    if sub == "status":
        st = ctl.call("screen.status")

        def human(st):
            scr = st.get("screen")
            print("Desktop : %s%s" % (st.get("display") or "none", "  %dx%d" % tuple(scr) if scr else ""))
            print("State   : %s%s" % (st["state"], " - %s" % st["error"] if st.get("error") else ""))
            print("Viewers : %d · quality %s" % (st["viewers"], st["quality"]))
            if st.get("stream"):
                s = st["stream"]
                print("Stream  : %sx%s@%s %s kbit/s" % (s["width"], s["height"], s["fps"], s["bitrate"] // 1000))
            print("Watch   : the web UI's Screen tab, or the app's Remote › Screen")
        out(a, st, human)
    elif sub == "quality":
        ctl.call("screen.settings.set", settings={"quality": a.value})
        print("screen quality: %s" % a.value)
    elif sub == "save":
        save_preview(ctl, a.file, a.seconds, "/ws/screen")


def cmd_call(ctl, a):
    resp = ctl.client.request(a.op, **json.loads(a.params))
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


# ------------------------------------------------------------ module commands
def cmd_camera(ctl, a):
    sub = a.cam_cmd or "sources"
    if sub == "sources":
        r = ctl.call("camera.sources")
        out(a, r, lambda r: [print("%s %-26s %-28s %s" % ("*" if s["id"] == r["current"] or r["current"].startswith(s["id"] + ":") or
                                                       (s["id"] == "test" and r["current"].startswith("test")) else " ",
                                                       s["id"], s["title"][:28], s.get("note") or ""))
                             for s in r["sources"]])
    elif sub == "select":
        st = ctl.call("camera.select", source=a.source, mode=a.mode, spec=a.spec, timeout=60)
        print("camera source: %s" % st.get("source"))


def cmd_net(ctl, a):
    sub = a.net_cmd or "status"
    if sub in ("status", "devices"):
        r = ctl.call("net.devices")
        out(a, r, lambda r: [print("%-14s %-9s %-14s %-24s %s" % (d["device"], d["type"], d["state"][:14],
                                                                ", ".join(d["ip4"]) or "-", d.get("connection") or ""))
                             for d in r["devices"]])
    elif sub == "connections":
        r = ctl.call("net.connections")
        out(a, r, lambda r: [print("%s %-28s %-16s %s %s" % ("*" if c["active"] else " ", c["name"][:28], c["type"][:16],
                                                           c["uuid"], c.get("device") or ""))
                             for c in r["connections"]])
    elif sub == "show":
        out(a, ctl.call("net.connection.get", uuid=a.uuid), lambda c: print(json.dumps(c, indent=2)))
    elif sub == "set":
        params = {"uuid": a.uuid}
        if a.method:
            params["ipv4_method"] = a.method
        for k in ("addresses", "gateway", "dns", "name", "mtu"):
            if getattr(a, k) is not None:
                params[k] = getattr(a, k)
        if a.autoconnect is not None:
            params["autoconnect"] = a.autoconnect == "yes"
        out(a, ctl.call("net.connection.set", **params), lambda c: print("saved %s (ipv4 %s %s)" % (
            c["name"], c["ipv4"]["method"], ", ".join(c["ipv4"]["addresses"]))))
    elif sub in ("up", "down", "delete"):
        print(ctl.call("net.connection." + sub, uuid=a.uuid)["message"])
    elif sub == "add-ethernet":
        params = {"interface": a.interface, "name": a.name, "ipv4_method": a.method}
        for k in ("addresses", "gateway", "dns"):
            if getattr(a, k) is not None:
                params[k] = getattr(a, k)
        out(a, ctl.call("net.connection.add_ethernet", **params), lambda c: print("added %s" % c.get("name")))


def cmd_bt(ctl, a):
    sub = a.bt_cmd or "status"
    if sub == "status":
        out(a, ctl.call("bt.status"), lambda s: print(
            "%s %s  powered %s  discoverable %s" % (s.get("alias") or s.get("name"), s.get("address"),
                                                     s.get("powered"), s.get("discoverable")) if s.get("present")
            else "no Bluetooth adapter: %s" % s.get("error")))
    elif sub == "power":
        ctl.call("bt.power", on=a.state == "on")
        print("Bluetooth %s" % a.state)
    elif sub in ("devices", "scan"):
        r = ctl.call("bt.scan", seconds=a.seconds, timeout=60) if sub == "scan" else ctl.call("bt.devices")
        out(a, r, lambda r: [print("%s  %-28s %s%s%s %s" % (d["address"], str(d.get("name"))[:28],
                                                          "paired " if d.get("paired") else "",
                                                          "trusted " if d.get("trusted") else "",
                                                          "CONNECTED" if d.get("connected") else "",
                                                          "rssi %s" % d["rssi"] if d.get("rssi") is not None else ""))
                             for d in r["devices"]] or print("no devices"))
    else:
        r = ctl.call("bt." + sub, address=a.address, timeout=60)
        print("%s %s: ok" % (sub, a.address))


def _hexdump(data, start=0):
    b = bytes(data)
    for i in range(0, len(b), 16):
        row = b[i:i + 16]
        print("%04x  %-48s %s" % (start + i, " ".join("%02x" % x for x in row),
                                  "".join(chr(x) if 32 <= x < 127 else "." for x in row)))


def cmd_io(ctl, a):
    sub = a.io_cmd or "info"
    if sub == "info":
        r = ctl.call("io.info")

        def human(r):
            print("Board : %s (%s)" % (r.get("board") or "generic", r.get("model")))
            print("GPIO  : " + ", ".join("%s(%s, %s lines)" % (c["chip"], c.get("label", "?"), c.get("lines", "?"))
                                         if "error" not in c else "%s: %s" % (c["chip"], c["error"]) for c in r["gpio"]))
            print("I2C   : " + (", ".join("i2c-%d %s" % (b["bus"], "/".join(b["dt"]) or b["name"]) for b in r["i2c"]) or "-"))
            print("SPI   : " + (", ".join(d["path"] for d in r["spi"]) or "none (enable an spidev overlay)"))
            print("UART  : " + ", ".join("%s%s" % (p["port"], " (console)" if p["console"] else "") for p in r["uart"]))
            print("PWM   : " + (", ".join("pwmchip%d %s" % (c["chip"], "/".join(c["dt"])) for c in r["pwm"]) or "-"))
            print("LEDs  : " + ", ".join(l["name"] for l in r["leds"]))
            print("ADC   : " + ", ".join("%s (%d ch)" % (d["name"], len(d["channels"])) for d in r["adc"]))
            for p in r.get("problems") or []:
                print("NOTE  : " + p)
        out(a, r, human)
    elif sub == "header":
        r = ctl.call("io.gpio.header")

        def human(r):
            if not r.get("board"):
                print("no pin map for this board")
                return
            pins = {p["pin"]: p for p in r["pins"]}

            def cell(p):
                if "gpio" not in p:
                    return p["name"]
                st = p.get("held") or p.get("state") or {}
                v = st.get("value")
                return "%s %s%s" % (p["name"], (st.get("direction") or st.get("mode") or "")[:3],
                                    "=%d" % v if v is not None else "")
            print("%s header" % r["board"])
            for n in range(1, 41, 2):
                print("%28s %2d | %-2d %s" % (cell(pins[n]), n, n + 1, cell(pins[n + 1])))
        out(a, r, human)
    elif sub == "gpio":
        g = a.gpio_cmd or "chips"
        if g == "chips":
            out(a, ctl.call("io.gpio.chips"), lambda r: [print("%s  %-12s %s lines" % (c["chip"], c.get("label"), c.get("lines")))
                                                          if "error" not in c else print(c["chip"], c["error"]) for c in r["chips"]])
        elif g == "lines":
            r = ctl.call("io.gpio.lines", chip=a.chip)
            out(a, r, lambda r: [print("%3d %-5s %-7s %-9s %-10s %s%s" % (
                l["line"], "pin%s" % l["pin"] if l.get("pin") else "", l["direction"], l["bias"],
                l["consumer"] or ("(held)" if l.get("held") else "-"),
                "value=%s " % l["value"] if "value" in l else "", "USED" if l["used"] else ""))
                for l in r["lines"]])
        elif g == "set":
            ctl.call("io.gpio.request", chip=a.chip, line=a.line, mode="output", value=a.value, drive=a.drive,
                     bias=a.bias, active_low=a.active_low)
            print("%s line %d = %d" % (a.chip, a.line, a.value))
        elif g == "get":
            r = ctl.call("io.gpio.request", chip=a.chip, line=a.line, mode="input", bias=a.bias,
                         active_low=a.active_low)
            print(r["value"])
        elif g == "watch":
            ctl.call("io.gpio.request", chip=a.chip, line=a.line, mode="input", bias=a.bias, edge=a.edge,
                     debounce_us=a.debounce_us)
            c = ctl.client
            seen = set()
            print("watching %s line %d for %s edges (Ctrl+C stops)" % (a.chip, a.line, a.edge), flush=True)

            def show(msg):
                if msg.get("ev") == "state" and msg.get("topic") == "io.gpio":
                    for e in msg["data"].get("events", []):
                        key = (e["chip"], e["line"], e["t"])
                        if key not in seen and e["line"] == a.line:
                            seen.add(key)
                            print("%.6f %s" % (e["t"], e["edge"]), flush=True)
            c.on_event = show
            try:
                while not c.closed:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        elif g == "release":
            ctl.call("io.gpio.release", chip=a.chip, line=a.line)
            print("released")
    elif sub == "i2c":
        i = a.i2c_cmd or "buses"
        if i == "buses":
            out(a, ctl.call("io.i2c.buses"), lambda r: [print("i2c-%-3d %-10s %s" % (b["bus"], "/".join(b["dt"]) or "-", b["name"]))
                                                         for b in r["buses"]])
        elif i == "scan":
            r = ctl.call("io.i2c.scan", bus=a.bus)

            def human(r):
                print("     " + " ".join("%2x" % c for c in range(16)))
                for row in range(0, 0x80, 16):
                    cells = []
                    for c in range(16):
                        ad = row + c
                        cells.append("%02x" % ad if ad in r["found"] else "UU" if ad in r["busy"] else
                                     "--" if 0x08 <= ad <= 0x77 else "  ")
                    print("%02x:  %s" % (row, " ".join(cells)))
            out(a, r, human)
        elif i == "read":
            r = ctl.call("io.i2c.transfer", bus=a.bus, addr=a.addr, write=a.reg, read=a.count)
            out(a, r, lambda r: print(r["read"]))
        elif i == "write":
            ctl.call("io.i2c.transfer", bus=a.bus, addr=a.addr, write=a.data)
            print("ok")
        elif i == "dump":
            r = ctl.call("io.i2c.dump", bus=a.bus, addr=a.addr, start=a.start, count=a.count)
            out(a, r, lambda r: _hexdump(r["bytes"], r["start"]))
    elif sub == "spi":
        if (a.spi_cmd or "devices") == "devices":
            out(a, ctl.call("io.spi.devices"), lambda r: [print(d["path"]) for d in r["devices"]] or
                print("no spidev devices (enable an spidev overlay)"))
        else:
            r = ctl.call("io.spi.transfer", device=a.device, tx=a.data, mode=a.mode, speed_hz=a.speed, bits=a.bits)
            out(a, r, lambda r: print(r["rx"]))
    elif sub == "uart":
        u = a.uart_cmd or "ports"
        if u == "ports":
            out(a, ctl.call("io.uart.ports"), lambda r: [print("%-10s %-14s %-8s %s%s" % (
                p["port"], p["driver"], p["kind"], "/".join(p["dt"]), " (console)" if p["console"] else ""))
                for p in r["ports"]])
        elif u == "open":
            r = ctl.call("io.uart.open", port=a.port, baud=a.baud, data_bits=a.data_bits, parity=a.parity,
                         stop_bits=a.stop_bits, flow=a.flow)
            if a.detached:
                print(r["term"])
                return
            interactive(ctl, r["term"])
        elif u == "send":
            r = ctl.call("io.uart.send", term=a.term, hex=a.hex) if a.hex else                 ctl.call("io.uart.send", term=a.term, text=a.text.encode().decode("unicode_escape"))
            print("sent %d bytes" % r["sent"])
    elif sub == "pwm":
        if (a.pwm_cmd or "list") == "list":
            out(a, ctl.call("io.pwm.list"), lambda r: [print("pwmchip%d %-8s %s" % (
                c["chip"], "/".join(c["dt"]), "  ".join(
                    "ch%d %s" % (ch["channel"], ("%s ns / %s ns %s%s" % (ch["duty_ns"], ch["period_ns"], ch["polarity"],
                                                                         " ON" if ch["enabled"] else " off"))
                                 if ch["exported"] else "-") for ch in c["channels"])))
                for c in r["chips"]])
        else:
            params = {"chip": a.chip, "channel": a.channel}
            if a.freq is not None:
                params["freq_hz"] = a.freq
            if a.duty is not None:
                params["duty_pct"] = a.duty
            if a.polarity:
                params["polarity"] = a.polarity
            if a.state:
                params["enabled"] = a.state == "on"
            out(a, ctl.call("io.pwm.set", **params), lambda r: print(json.dumps(r)))
    elif sub == "led":
        if a.name is None:
            out(a, ctl.call("io.led.list"), lambda r: [print("%-14s %3s/%-3s %s" % (l["name"], l["brightness"], l["max"], l["trigger"]))
                                                        for l in r["leds"]])
        else:
            out(a, ctl.call("io.led.set", name=a.name, brightness=a.brightness, trigger=a.trigger),
                lambda l: print("%s %s/%s %s" % (l["name"], l["brightness"], l["max"], l["trigger"])))
    elif sub == "adc":
        out(a, ctl.call("io.adc.read"), lambda r: [print("%s ch%-3s raw %5s  %8s mV" % (d["name"], c["channel"], c["raw"], c["mv"]))
                                                   for d in r["devices"] for c in d["channels"]])


def _files_url(ctl, path, query):
    base, token = ctl.http_base()
    return "%s/api/files/%s?%s" % (base, path, urllib.parse.urlencode(query)), token


def cmd_files(ctl, a):
    sub = a.files_cmd or "ls"
    if sub == "ls":
        r = ctl.call("files.list", path=a.path, hidden=a.all)
        out(a, r, lambda r: [print("%s %10s %s %s%s" % (e["mode"], fmt_size(e["size"]) if e["type"] == "file" else "",
                                                        time.strftime("%F %H:%M", time.localtime(e["mtime"])), e["name"],
                                                        "/" if e["type"] == "dir" else ""))
                             for e in r["entries"]])
    elif sub == "roots":
        out(a, ctl.call("files.roots"), lambda r: [print(x["path"]) for x in r["roots"]])
    elif sub == "get":
        url, token = _files_url(ctl, "download", {"path": a.path})
        dest = a.out or os.path.basename(a.path)
        if os.path.isdir(dest):
            dest = os.path.join(dest, os.path.basename(a.path))
        req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token} if token else {})
        with urllib.request.urlopen(req, timeout=30) as r, open(dest + ".part", "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(dest + ".part", dest)
        print("saved %s (%s)" % (dest, fmt_size(os.path.getsize(dest))))
    elif sub == "put":
        name = a.name or os.path.basename(a.file)
        ctl.call("files.upload_check", dir=a.dir, name=name, overwrite=a.force)
        url, token = _files_url(ctl, "upload", {"dir": a.dir, "name": name, "overwrite": "1" if a.force else "0"})
        size = os.path.getsize(a.file)
        headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
        if token:
            headers["Authorization"] = "Bearer %s" % token
        with open(a.file, "rb") as f:
            req = urllib.request.Request(url, data=f, headers=headers, method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    res = json.loads(r.read())
            except urllib.error.HTTPError as e:
                res = json.loads(e.read() or b"{}")
            except urllib.error.URLError as e:
                sys.exit("upload failed: %s" % e.reason)
        if not res.get("ok"):
            sys.exit("upload failed: %s" % res.get("error"))
        print("uploaded %s (%s)" % (res["data"]["path"], fmt_size(size)))
    elif sub == "rm":
        print("deleted " + ctl.call("files.delete", path=a.path, recursive=a.recursive)["deleted"])
    elif sub == "mkdir":
        print("created " + ctl.call("files.mkdir", path=a.path)["path"])
    elif sub == "mv":
        print("renamed to " + ctl.call("files.rename", path=a.path, to=a.to)["path"])
    elif sub == "cat":
        r = ctl.call("files.read", path=a.path)
        sys.stdout.write(r["text"])


def cmd_system(ctl, a):
    sub = a.sys_cmd or "info"
    if sub == "info":
        r = ctl.call("system.info")

        def human(r):
            print("Arstro Remote %s  slot %s  port %s  pid %s" % (r["version"], (r["slot"] or "-").upper(), r["port"], r["pid"]))
            print("Host    : %s (%s, %s, kernel %s)" % (r["hostname"], r.get("board") or r.get("model"), r["machine"], r["kernel"]))
            print("Paths   : code %s" % r["paths"]["code"])
            print("          config %s  logs %s" % (r["paths"]["config"], r["paths"]["logs"]))
            print("Log     : level %s" % r["log_level"])
            for m in r["modules"]:
                print("  %-10s %-9s %s%s" % (m["name"], m["state"], m["description"], " (%s)" % m["note"] if m.get("note") else ""))
        out(a, r, human)
    elif sub == "modules":
        if a.set:
            r = ctl.call("system.modules.set", modules=[m.strip() for m in a.set.split(",") if m.strip()])
            print("modules: %s - the server restarts" % ", ".join(r["modules"]))
        else:
            out(a, ctl.call("system.modules"), lambda r: [print("%-10s %-9s %s" % (m["name"], m["state"], m["description"]))
                                                           for m in r["modules"]])
    elif sub in ("restart", "reboot", "poweroff"):
        if sub != "restart" and not a.yes:
            sys.exit("this will %s the machine - add --yes" % sub)
        ctl.call("system." + sub)
        print("%s requested" % sub)


def cmd_log(ctl, a):
    if a.level:
        r = ctl.call("log.level", level=a.level, save=a.save)
        print("log level: %s" % r["level"])
        return
    if a.list:
        out(a, ctl.call("log.files"), lambda r: [print("%-16s %10s  %s" % (f["name"], fmt_size(f["size"]), f["path"]))
                                                 for f in r["files"]])
        return
    last = []
    while True:
        r = ctl.call("log.tail", file=a.file, lines=a.lines, grep=a.grep)
        lines = r["lines"]
        if last:
            # print only what is new since the last poll
            try:
                idx = len(lines) - 1 - lines[::-1].index(last[-1])
                lines = lines[idx + 1:]
            except ValueError:
                pass
        for l in lines:
            print(l)
        sys.stdout.flush()
        last = r["lines"] or last
        if not a.follow:
            return
        time.sleep(1)


def cmd_apps(ctl, a):
    sub = a.apps_cmd or "list"
    if sub == "spec":                                   # the protocol itself: no server needed
        from .ntwb import spec
        sys.stdout.write(spec.render_json() if a.json else spec.render_markdown())
        return
    if sub == "list":
        r = ctl.call("apps.list")

        def human(r):
            print("NTWB %s · socket %s" % (r["ntwb"], r["socket"]))
            if not r["apps"]:
                print("no apps installed (manifests go in %s)" % r["search"][0])
            for x in r["apps"]:
                print("%-12s %-8s %-9s %-22s %s%s" % (x["id"] or "?", x.get("version") or "", x["state"],
                                                     str(x.get("name"))[:22], "clients %d" % x["clients"] if x.get("clients") else "",
                                                     "  PROBLEM: %s" % x["problem"] if x.get("problem") else ""))
        out(a, r, human)
    elif sub in ("info", "api"):
        out(a, ctl.call("apps." + sub, app=a.id), lambda r: print(json.dumps(r, indent=2)))
    elif sub == "launch":
        r = ctl.call("apps.launch", app=a.id, timeout=60)
        print("%s: %s%s" % (a.id, r["state"], " - %s" % r["detail"] if r.get("detail") else ""))
    elif sub == "stop":
        r = ctl.call("apps.stop", app=a.id, timeout=60)
        print("%s: %s" % (a.id, r.get("state", "stopped")))
    elif sub == "call":
        params = json.loads(a.params) if a.params else {}
        r = ctl.call("apps.call", app=a.id, method=a.method, params=params, timeout=a.timeout + 5)
        print(json.dumps(r, indent=2))
    elif sub == "state":
        r = ctl.call("apps.state", app=a.id, **({"key": a.key} if a.key else {}))
        print(json.dumps(r, indent=2))
    elif sub == "log":
        for line in ctl.call("apps.log", app=a.id, lines=a.lines)["lines"]:
            print(line)
    elif sub == "register":
        r = ctl.call("apps.register", path=os.path.abspath(a.path))
        print("registered %s%s" % (r.get("id"), " (problem: %s)" % r["problem"] if r.get("problem") else ""))
    elif sub == "unregister":
        ctl.call("apps.unregister", path=os.path.abspath(a.path))
        print("unregistered")


def add_module_parsers(sub):
    ap_ = sub.add_parser("apps", help="native apps with a web UI (NTWB)")
    aps = ap_.add_subparsers(dest="apps_cmd")
    aps.add_parser("list")
    aps.add_parser("spec", help="print the NTWB protocol reference (--json: the catalogue)")
    for name in ("info", "api", "launch", "stop", "log", "state", "call"):
        x = aps.add_parser(name)
        x.add_argument("id")
        if name == "log":
            x.add_argument("-n", "--lines", type=int, default=100)
        if name == "state":
            x.add_argument("key", nargs="?")
        if name == "call":
            x.add_argument("method")
            x.add_argument("params", nargs="?", default="{}", help="JSON object")
            x.add_argument("--timeout", type=float, default=60)
    for name in ("register", "unregister"):
        x = aps.add_parser(name)
        x.add_argument("path", help="an ntwb.json manifest or its directory")

    cam = sub.add_parser("camera", help="camera source (HDMI input, USB camera, test pattern)")
    cs = cam.add_subparsers(dest="cam_cmd")
    cs.add_parser("sources")
    sel = cs.add_parser("select")
    sel.add_argument("source", help="hdmi | test | v4l2:/dev/videoN")
    sel.add_argument("--mode", help="camera mode, e.g. 'MJPG 1280x720@30'")
    sel.add_argument("--spec", help="test pattern size, e.g. 1920x1080@30")

    n = sub.add_parser("net", help="network devices and connection profiles (Ethernet, Wi-Fi)")
    ns = n.add_subparsers(dest="net_cmd")
    ns.add_parser("status")
    ns.add_parser("devices")
    ns.add_parser("connections")
    for name in ("show", "up", "down", "delete"):
        x = ns.add_parser(name)
        x.add_argument("uuid")
    st = ns.add_parser("set", help="change a profile, e.g. --method manual --addresses 192.0.2.50/24 --gateway 192.0.2.1")
    st.add_argument("uuid")
    ae = ns.add_parser("add-ethernet", help="new Ethernet profile")
    ae.add_argument("interface")
    ae.add_argument("--name")
    for x in (st, ae):
        x.add_argument("--method", choices=["auto", "manual", "disabled", "shared", "link-local"])
        x.add_argument("--addresses")
        x.add_argument("--gateway")
        x.add_argument("--dns")
    st.add_argument("--name")
    st.add_argument("--mtu")
    st.add_argument("--autoconnect", choices=["yes", "no"])

    b = sub.add_parser("bt", help="Bluetooth devices (keyboards, headsets, ...)")
    bs = b.add_subparsers(dest="bt_cmd")
    bs.add_parser("status")
    bp = bs.add_parser("power")
    bp.add_argument("state", choices=["on", "off"])
    bs.add_parser("devices")
    bsc = bs.add_parser("scan")
    bsc.add_argument("--seconds", type=int, default=8)
    for name in ("pair", "connect", "disconnect", "trust", "untrust", "remove"):
        x = bs.add_parser(name)
        x.add_argument("address")

    io = sub.add_parser("io", help="IO control: GPIO, I2C, SPI, UART, PWM, LEDs, ADC")
    ios = io.add_subparsers(dest="io_cmd")
    ios.add_parser("info")
    ios.add_parser("header", help="the board's pin header with live states")
    g = ios.add_parser("gpio")
    gs = g.add_subparsers(dest="gpio_cmd")
    gs.add_parser("chips")
    gl = gs.add_parser("lines")
    gl.add_argument("chip", help="gpiochip number or label (gpio1)")
    for name in ("set", "get", "watch", "release"):
        x = gs.add_parser(name)
        x.add_argument("chip")
        x.add_argument("line", type=int)
        if name == "set":
            x.add_argument("value", type=int, choices=[0, 1])
            x.add_argument("--drive", default="push-pull", choices=["push-pull", "open-drain", "open-source"])
        if name in ("set", "get", "watch"):
            x.add_argument("--bias", default="as-is", choices=["as-is", "pull-up", "pull-down", "disabled"])
        if name in ("set", "get"):
            x.add_argument("--active-low", action="store_true")
        if name == "watch":
            x.add_argument("--edge", default="both", choices=["rising", "falling", "both"])
            x.add_argument("--debounce-us", type=int, default=0)
    i2 = ios.add_parser("i2c")
    i2s = i2.add_subparsers(dest="i2c_cmd")
    i2s.add_parser("buses")
    sc = i2s.add_parser("scan")
    sc.add_argument("bus")
    rd = i2s.add_parser("read", help="write REG bytes, then read COUNT (repeated start)")
    rd.add_argument("bus")
    rd.add_argument("addr")
    rd.add_argument("reg", nargs="?", default="", help="hex bytes to write first, e.g. '00' or '10 02'")
    rd.add_argument("--count", type=int, default=1)
    wr = i2s.add_parser("write")
    wr.add_argument("bus")
    wr.add_argument("addr")
    wr.add_argument("data", help="hex bytes, e.g. '10 ff'")
    dp = i2s.add_parser("dump")
    dp.add_argument("bus")
    dp.add_argument("addr")
    dp.add_argument("--start", type=lambda v: int(v, 0), default=0)
    dp.add_argument("--count", type=int, default=256)
    sp = ios.add_parser("spi")
    sps = sp.add_subparsers(dest="spi_cmd")
    sps.add_parser("devices")
    xf = sps.add_parser("xfer")
    xf.add_argument("device", help="spidev0.0 or /dev/spidev0.0")
    xf.add_argument("data", help="hex bytes to clock out")
    xf.add_argument("--mode", type=int, default=0, choices=[0, 1, 2, 3])
    xf.add_argument("--speed", type=int, default=1_000_000, help="Hz")
    xf.add_argument("--bits", type=int, default=8)
    ua = ios.add_parser("uart")
    uas = ua.add_subparsers(dest="uart_cmd")
    uas.add_parser("ports")
    uo = uas.add_parser("open", help="open a serial console (Ctrl+] detaches)")
    uo.add_argument("port")
    uo.add_argument("--baud", type=int, default=115200)
    uo.add_argument("--data-bits", type=int, default=8)
    uo.add_argument("--parity", default="none", choices=["none", "even", "odd"])
    uo.add_argument("--stop-bits", type=int, default=1, choices=[1, 2])
    uo.add_argument("--flow", default="none", choices=["none", "rtscts", "xonxoff"])
    uo.add_argument("--detached", action="store_true", help="just open it and print the terminal number")
    us = uas.add_parser("send")
    us.add_argument("term", type=int)
    ug = us.add_mutually_exclusive_group(required=True)
    ug.add_argument("--hex")
    ug.add_argument("--text", help="text (\r \n escapes work)")
    pw = ios.add_parser("pwm")
    pws = pw.add_subparsers(dest="pwm_cmd")
    pws.add_parser("list")
    ps = pws.add_parser("set")
    ps.add_argument("chip")
    ps.add_argument("channel", type=int, nargs="?", default=0)
    ps.add_argument("--freq", type=float, help="Hz")
    ps.add_argument("--duty", type=float, help="percent")
    ps.add_argument("--polarity", choices=["normal", "inversed"])
    ps.add_argument("state", nargs="?", choices=["on", "off"])
    le = ios.add_parser("led")
    le.add_argument("name", nargs="?")
    le.add_argument("--brightness", type=int)
    le.add_argument("--trigger")
    ios.add_parser("adc")

    f = sub.add_parser("files", help="browse, upload and download files")
    fs = f.add_subparsers(dest="files_cmd")
    fl = fs.add_parser("ls")
    fl.add_argument("path", nargs="?")
    fl.add_argument("-a", "--all", action="store_true")
    fs.add_parser("roots")
    fg = fs.add_parser("get")
    fg.add_argument("path")
    fg.add_argument("--out")
    fp = fs.add_parser("put")
    fp.add_argument("file")
    fp.add_argument("dir")
    fp.add_argument("--name")
    fp.add_argument("--force", action="store_true", help="overwrite")
    fr = fs.add_parser("rm")
    fr.add_argument("path")
    fr.add_argument("-r", "--recursive", action="store_true")
    fm = fs.add_parser("mkdir")
    fm.add_argument("path")
    fv = fs.add_parser("mv")
    fv.add_argument("path")
    fv.add_argument("to")
    fc = fs.add_parser("cat")
    fc.add_argument("path")

    sy = sub.add_parser("system", help="server info, modules, restart, reboot")
    sys_ = sy.add_subparsers(dest="sys_cmd")
    sys_.add_parser("info")
    sm = sys_.add_parser("modules")
    sm.add_argument("--set", help="comma-separated modules to enable (the server restarts)")
    sys_.add_parser("restart")
    for name in ("reboot", "poweroff"):
        x = sys_.add_parser(name)
        x.add_argument("--yes", action="store_true")

    lg = sub.add_parser("log", help="the server log")
    lg.add_argument("--file", help="arstro-remote (default), recorder, screen, launcher, crash")
    lg.add_argument("-n", "--lines", type=int, default=100)
    lg.add_argument("--grep")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("--list", action="store_true", help="list the log files")
    lg.add_argument("--level", choices=["debug", "info", "warning", "error"], help="change the log level now")
    lg.add_argument("--save", action="store_true", help="with --level: keep it after a restart")


# ---------------------------------------------------------------------- parser
def build_parser():
    ap = argparse.ArgumentParser(prog="arstro-remote", description="Arstro Remote - Orange Pi 5 Plus controller",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--url", help="server URL for remote use, e.g. http://192.0.2.10:8080 (env ARSTRO_URL)")
    ap.add_argument("--token", help="access password for --url (env ARSTRO_TOKEN; `arstro-remote web --show`)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--slot", help="the instance (slot a, b, ...) on this machine (env ARSTRO_SLOT)")
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
    wb = sub.add_parser("web", help="web access: URLs, password, open/closed")
    wb.add_argument("--show", action="store_true", help="print the password")
    wb.add_argument("--set-password", nargs="?", const="", metavar="PASSWORD",
                    help="set your own password (asks when no value; '-' reads it from stdin)")
    wb.add_argument("--rotate", action="store_true", help="replace the password with a random one")
    wg = wb.add_mutually_exclusive_group()
    wg.add_argument("--open", action="store_true", help="no password at all (anyone on the network)")
    wg.add_argument("--require-password", action="store_true", help="need the password again")
    sst = sub.add_parser("stats", help="system monitor snapshot")
    sst.add_argument("--watch", action="store_true", help="print a line every 2 s (Ctrl+C stops)")

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
    so = rs.add_parser("source", help="HDMI RX or a test pattern (no argument: show it)")
    sg = so.add_mutually_exclusive_group()
    sg.add_argument("--hdmi", action="store_true", help="record the HDMI input")
    sg.add_argument("--test", metavar="WxH@FPS", help="test pattern, e.g. 1920x1080@30")
    pv = rs.add_parser("preview", help="save the live preview to an .h264 file")
    pv.add_argument("file")
    pv.add_argument("--seconds", type=float, default=5)

    sc_ = sub.add_parser("screen", help="remote screen: the Pi's desktop")
    scs = sc_.add_subparsers(dest="screen_cmd")
    scs.add_parser("status")
    sq = scs.add_parser("quality", help="stream quality (shared)")
    sq.add_argument("value", choices=["low", "medium", "high"])
    sv = scs.add_parser("save", help="save the live desktop to an .h264 file")
    sv.add_argument("file")
    sv.add_argument("--seconds", type=float, default=5)
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
    cv.add_argument("--keep-raw", action="store_true",
                    help="FFV1 from RAW: keep the RAW (default follows raw.ffv1_replace_raw: check, then delete)")
    vf = gs.add_parser("verify", help="compare a take's FFV1 with its RAW byte for byte")
    vf.add_argument("file", help="the FFV1 (or RAW) file of the take")
    vf.add_argument("--delete-raw", action="store_true", help="delete the RAW if every frame and sample match")
    vf.add_argument("--wait", action="store_true", help="wait and show progress")
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

    add_module_parsers(sub)

    sl = sub.add_parser("slots", help="A/B slots: which hosts this session, which is idle")
    sl.add_argument("--current", action="store_true", help="print the slot hosting this session")
    sl.add_argument("--idle", action="store_true", help="print the slot to deploy into")

    ca = sub.add_parser("call", help="send any protocol op")
    ca.add_argument("op")
    ca.add_argument("params", nargs="?", default="{}")
    sub.add_parser("version")
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if getattr(a, "slot", None):
        os.environ["ARSTRO_SLOT"] = a.slot.lower()
    if a.cmd == "run":
        from .daemon import Daemon, load_config, setup_logging
        cfg = load_config()
        setup_logging(a.debug, cfg.get("log_level"))
        if a.no_web:
            cfg["web_enabled"] = False
        if a.no_recorder:
            cfg["recorder_enabled"] = False
        if a.simulate:
            cfg["recorder_simulate"] = a.simulate
        if a.web_port:
            cfg["web_port"] = a.web_port
        return Daemon(cfg, use_bluetooth=not a.no_bluetooth).run()
    if a.cmd == "slots":                       # no server needed: /proc and the lock files
        from . import slots as slots_mod
        if a.current:
            print(slots_mod.current() or "-")
        elif a.idle:
            print(slots_mod.idle() or "-")
        else:
            rows = slots_mod.slots()
            if a.json:
                print(json.dumps({"slots": rows, "current": slots_mod.current(), "idle": slots_mod.idle()}, indent=2))
            else:
                for r in rows:
                    print("%-7s port %-5d %-8s %-10s %s" % (r["slot"], r["port"], "running" if r["running"] else "stopped",
                                                           "pid %s" % r["pid"] if r["pid"] else "",
                                                           "<- THIS SESSION (do not reinstall/restart)" if r["current"] else ""))
                print("deploy into: %s" % (slots_mod.idle() or "-"))
        return 0
    if a.cmd == "version" or a.cmd is None:
        if a.cmd is None:
            ap.print_help()
        else:
            print(__version__)
        return 0
    ctl = Ctl(a)
    handlers = {"status": cmd_status, "watch": cmd_watch, "pair": cmd_pair, "unpair": cmd_unpair,
                "web": cmd_web, "screen": cmd_screen, "stats": cmd_stats, "wifi": cmd_wifi, "term": cmd_term, "input": cmd_input,
                "rec": cmd_rec, "gallery": cmd_gallery, "jobs": cmd_jobs, "call": cmd_call,
                "camera": cmd_camera, "net": cmd_net, "bt": cmd_bt, "io": cmd_io, "files": cmd_files,
                "system": cmd_system, "log": cmd_log, "apps": cmd_apps}
    try:
        return handlers[a.cmd](ctl, a) or 0
    except KeyboardInterrupt:
        return 130
