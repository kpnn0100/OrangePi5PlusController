"""Module framework, System, Logs, Files and IO Control - against a throwaway instance.

The suite starts its own server (slot "t", temporary config/log/state dirs, a free port,
no Bluetooth, only the monitor/system/io/files modules), so it never touches the installed
slots and runs anywhere. Hardware writes are opt-in:

    ARSTRO_TEST_GPIO=gpiochip1:30   also request/read/release that (free!) GPIO line

    python3 server/tests/test_modules.py [-k NAME]
"""

import json
import os
import pty
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from harness import check, run, test, wait_until

from arstro_remote.client import Client

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["monitor", "system", "io", "files"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Instance:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="arstro-test-")
        self.port = free_port()
        self.token = secrets.token_urlsafe(12)
        self.files = os.path.join(self.dir, "files")
        os.makedirs(self.files)
        self.env = dict(os.environ, ARSTRO_SLOT="t", XDG_CONFIG_HOME=self.dir + "/cfg", XDG_STATE_HOME=self.dir + "/state",
                        XDG_DATA_HOME=self.dir + "/data", XDG_RUNTIME_DIR=self.dir + "/run", PYTHONPATH=SERVER)
        os.makedirs(self.dir + "/run", mode=0o700)
        cfg = os.path.join(self.dir, "cfg", "arstro-remote-t")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "config.json"), "w") as f:
            json.dump({"web_port": self.port, "web_host": "127.0.0.1", "bluetooth_enabled": False, "modules": MODULES,
                       "files_roots": [self.files], "log_level": "info"}, f)
        fd = os.open(os.path.join(cfg, "web_token"), os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, (self.token + "\n").encode())
        os.close(fd)
        self.log = os.path.join(self.dir, "state", "arstro-remote-t", "arstro-remote.log")
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "arstro_remote", "run"], cwd=SERVER, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sock = os.path.join(self.dir, "run", "arstro-remote-t.sock")
        if not wait_until(lambda: os.path.exists(sock) and self._ping(), 20):
            raise RuntimeError("test instance did not start (see %s)" % self.log)
        self.c = Client.unix(sock)
        self.hello = self.c.call("hello", app="test-modules")

    def _ping(self):
        try:
            urllib.request.urlopen(self.base + "/api/ping", timeout=1).read()
            return True
        except OSError:
            return False

    def http(self, method, path, data=None, headers=None):
        h = {"Authorization": "Bearer " + self.token}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stop(self):
        try:
            self.c.close()
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------- modules
@test("MOD-01", "MOD-04", "ADM-06", "ARC-08")
def hello_reports_the_enabled_modules(t):
    mods = {m["name"]: m for m in t.hello["modules"]}
    check(set(mods) >= {"monitor", "system", "camera", "connection", "terminal", "screen", "io", "files"}, mods)
    check({n for n, m in mods.items() if m["state"] != "disabled"} == set(MODULES), mods)
    check(t.hello["slot"] == "t", t.hello.get("slot"))
    check("terminal" not in t.hello["features"] and "stats" in t.hello["features"], t.hello["features"])


@test("MOD-01")
def ops_of_a_disabled_module_are_refused(t):
    for op, params in (("term.open", {"cols": 80, "rows": 24}), ("wifi.status", {}), ("recorder.status", {}),
                       ("in.pointer", {}), ("net.devices", {})):
        r = t.c.request(op, **params)
        check(r["ok"] is False and "not enabled" in r["error"], (op, r))
    check(t.c.call("stats.get")["cpu"], "monitor op failed")


@test("ADM-04", "MOD-03")
def system_info_names_slot_board_and_paths(t):
    i = t.c.call("system.info")
    check(i["slot"] == "t" and i["port"] == t.port, i)
    check(i["paths"]["logs"].startswith(t.dir) and i["paths"]["config"].startswith(t.dir), i["paths"])
    check([m["name"] for m in i["modules"] if m["state"] == "enabled"] == MODULES, i["modules"])
    st = t.c.call("admin.status")
    check(st["bluetooth"].get("disabled") and st["slot"] == "t", st["bluetooth"])


# ------------------------------------------------------------------------ logs
@test("LOG-01", "LOG-02", "LOG-03")
def failures_and_markers_are_in_the_log(t):
    t.c.request("files.list", path="/definitely/not/shared")
    t.c.call("log.mark", text="MARKER-%d" % os.getpid())
    files = {f["name"] for f in t.c.call("log.files")["files"]}
    check("arstro-remote" in files, files)
    lines = t.c.call("log.tail", lines=200)["lines"]
    check(any("MARKER-%d" % os.getpid() in l for l in lines), "marker missing")
    check(any("WARNING" in l and "files.list" in l and "outside the shared folders" in l for l in lines),
          "failed op not logged with its op name")
    only = t.c.call("log.tail", lines=50, grep="MARKER-%d" % os.getpid())["lines"]
    check(len(only) == 1, only)
    check(t.c.call("log.level", level="debug")["level"] == "debug", "level not changed")
    t.c.call("ping")
    check(t.c.call("log.level", level="info")["level"] == "info", "level not restored")
    check(t.token not in open(t.log, encoding="utf-8", errors="replace").read(), "the password is in the log")


# ----------------------------------------------------------------------- files
@test("FILE-01", "FILE-02", "FILE-03", "FILE-04")
def files_upload_browse_download_edit_delete(t):
    d = t.c.call("files.mkdir", dir=t.files, name="up")["path"]
    data = os.urandom(300_000)
    chk = t.c.call("files.upload_check", dir=d, name="blob.bin")
    check(not chk["exists"], chk)
    st, body = t.http("PUT", "/api/files/upload?dir=%s&name=blob.bin" % urllib.request.quote(d), data,
                      {"Content-Type": "application/octet-stream"})
    check(st == 200 and json.loads(body)["data"]["size"] == len(data), body[:200])
    st, body = t.http("PUT", "/api/files/upload?dir=%s&name=blob.bin" % urllib.request.quote(d), b"x")
    check(st == 400 and b"already exists" in body, body[:200])
    names = [e["name"] for e in t.c.call("files.list", path=d)["entries"]]
    check(names == ["blob.bin"], names)
    st, got = t.http("GET", "/api/files/download?path=%s" % urllib.request.quote(d + "/blob.bin"))
    check(st == 200 and got == data, "download differs")
    t.c.call("files.write", path=d + "/note.txt", text="hello\n")
    check(t.c.call("files.read", path=d + "/note.txt")["text"] == "hello\n", "text round trip")
    t.c.call("files.rename", path=d + "/note.txt", to="note2.txt")
    check(os.path.exists(d + "/note2.txt"), "rename")
    for bad in ("/etc", t.files + "/../", d + "/../../"):
        r = t.c.request("files.list", path=bad)
        check(r["ok"] is False and "outside" in r["error"], (bad, r))
    st, _ = t.http("GET", "/api/files/download?path=/etc/hostname")
    check(st == 404, "download outside the roots allowed")
    r = t.c.request("files.delete", path=d)
    check(r["ok"] is False and "not empty" in r["error"], r)
    t.c.call("files.delete", path=d, recursive=True)
    check(not os.path.exists(d), "delete")
    r = t.c.request("files.delete", path=t.files)
    check(r["ok"] is False, "a shared root must not be deletable")


# -------------------------------------------------------------------------- IO
@test("IO-01", "IO-02", "IO-04", "IO-05", "IO-06", "IO-07", "IO-08", "IO-10", "MOD-03", "ARC-08")
def io_inventory_through_standard_interfaces(t):
    info = t.c.call("io.info")
    for key in ("gpio", "i2c", "spi", "uart", "pwm", "leds", "adc", "problems"):
        check(key in info, key)
    check(all(c["chip"].startswith("/dev/gpiochip") for c in info["gpio"]), info["gpio"])
    ok_chips = [c for c in info["gpio"] if "error" not in c]
    if ok_chips:
        lines = t.c.call("io.gpio.lines", chip=ok_chips[0]["chip"])["lines"]
        check(len(lines) == ok_chips[0]["lines"] and {"line", "direction", "used"} <= set(lines[0]), lines[:1])
    hdr = t.c.call("io.gpio.header")
    if info.get("board"):
        check(len(hdr["pins"]) == 40 and sum(1 for p in hdr["pins"] if "gpio" in p) >= 20, hdr["board"])
    for d in t.c.call("io.adc.read")["devices"]:
        check(all("raw" in c for c in d["channels"]), d)
    check(isinstance(t.c.call("io.led.list")["leds"], list) and isinstance(t.c.call("io.pwm.list")["chips"], list), "lists")
    r = t.c.request("io.i2c.transfer", bus="nope", addr=0x50, read=1)
    check(r["ok"] is False and "bus" in r["error"], r)
    r = t.c.request("io.spi.transfer", device="spidev9.9", tx="00")
    check(r["ok"] is False, r)
    print("      board %s · %d GPIO chips (%d readable) · %d I2C · %d UART · %d PWM · %d LEDs" % (
        info.get("board") or "generic", len(info["gpio"]), len(ok_chips), len(info["i2c"]), len(info["uart"]),
        len(info["pwm"]), len(info["leds"])))


@test("IO-09", "TERM-02", "MOD-01")
def uart_console_is_a_shared_terminal(t):
    master, slave = pty.openpty()
    port = os.ttyname(slave)
    try:
        r = t.c.call("io.uart.open", port=port, baud=115200)
        tid = r["term"]
        check(r["settings"]["baud"] == 115200, r)
        again = t.c.call("io.uart.open", port=port)
        check(again["term"] == tid and again.get("already_open"), again)
        t.c.call("term.attach", term=tid, cols=80, rows=24)         # allowed: it is a serial console
        os.write(master, b"hello-from-device\r\n")
        check(wait_until(lambda: b"hello-from-device" in bytes(t.c.term_output.get(tid, b"")), 5),
              "device output did not reach the controller")
        t.c.call("io.uart.send", term=tid, hex="41 42 0d")
        got = b""
        end = time.time() + 5
        while b"AB\r" not in got and time.time() < end:
            got += os.read(master, 64)
        check(b"AB\r" in got, got)
        t.c.term_write(tid, b"typed")
        got = b""
        end = time.time() + 5
        while b"typed" not in got and time.time() < end:
            got += os.read(master, 64)
        check(b"typed" in got, got)
        cfg = t.c.call("io.uart.config", term=tid, baud=9600, parity="even")["settings"]
        check(cfg["baud"] == 9600 and cfg["parity"] == "even", cfg)
        term = [x for x in t.c.call("term.list")["terminals"] if x["term"] == tid][0]
        check(term["kind"] == "serial" and term["port"] == os.path.basename(port) and term["rx_bytes"] >= 19, term)
        r = t.c.request("io.uart.open", port=port, baud=12345)
        check(r["ok"] is True, "an open port is reused, whatever the settings")
        t.c.call("term.close", term=tid)
        check(not [x for x in t.c.call("term.list")["terminals"] if x["term"] == tid], "console not closed")
        r = t.c.request("io.uart.open", port="/etc/passwd")
        check(r["ok"] is False and "no serial port" in r["error"], r)
    finally:
        os.close(master)
        os.close(slave)


@test("IO-03")
def gpio_line_request_read_release(t):
    spec = os.environ.get("ARSTRO_TEST_GPIO")
    if not spec:
        print("      (skipped: set ARSTRO_TEST_GPIO=gpiochipN:LINE to a free line to run it)")
        return
    chip, line = spec.split(":")
    r = t.c.call("io.gpio.request", chip=chip, line=int(line), mode="input", bias="pull-up", edge="both")
    check(r["mode"] == "input" and r["value"] in (0, 1), r)
    check(wait_until(lambda: any(h["line"] == int(line) for h in (t.c.state.get("io.gpio") or {}).get("held", [])), 3),
          "io.gpio topic does not list the line")
    info = t.c.call("io.gpio.lines", chip=chip)["lines"][int(line)]
    check(info["held"] and info["consumer"] == "arstro-remote" and info["bias"] == "pull-up", info)
    r = t.c.request("io.gpio.set", chip=chip, line=int(line), value=1)
    check(r["ok"] is False and "input" in r["error"], r)
    t.c.call("io.gpio.release", chip=chip, line=int(line))
    info = t.c.call("io.gpio.lines", chip=chip)["lines"][int(line)]
    check(not info["used"], info)


# --------------------------------------------------------------- last: restart
@test("ADM-05", "MOD-02")
def modules_change_restarts_the_server(t):
    r = t.c.call("system.modules.set", modules=["monitor", "system", "files"], restart=True)
    check(r["restarting"] and r["modules"] == ["monitor", "system", "files"], r)
    check(wait_until(lambda: t.proc.poll() is not None, 30), "server did not stop for the restart")
    check(t.proc.returncode == 0, "exit code %s (the launcher restarts on any code)" % t.proc.returncode)
    t.start()                                      # what the launcher does
    on = {m["name"] for m in t.hello["modules"] if m["state"] != "disabled"}
    check(on == {"monitor", "system", "files"}, on)
    r = t.c.request("io.info")
    check(r["ok"] is False and "not enabled" in r["error"], r)


def setup():
    t = Instance()
    t.start()
    return t


if __name__ == "__main__":
    sys.exit(run(setup, lambda t: t.stop(), __doc__))
