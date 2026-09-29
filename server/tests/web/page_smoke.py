"""Web UI smoke test without Node: open every page in Chromium (DevTools protocol), report
JS errors / console errors / horizontal overflow per page and save a screenshot of each.

For machines where the full puppeteer suite (ui_test.mjs) cannot run - e.g. on the board
itself. Chromium runs as a normal window placed off-screen when the distro build has no
headless mode, so it needs an X display (DISPLAY=:0).

    ARSTRO_TOKEN=<password> DISPLAY=:0 python3 server/tests/web/page_smoke.py \
        http://127.0.0.1:8081 /tmp/shots [390x844] [route,route...]

(ARSTRO_TOKEN read at run time, e.g. `ARSTRO_TOKEN=$(cat ~/.config/arstro-remote-b/web_token)`.)
Exit code 1 if any page had errors or overflowed.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.environ.get("ARSTRO_SERVER_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from arstro_remote.web import ws  # noqa: E402

base, outdir = sys.argv[1].rstrip("/"), sys.argv[2]
password = os.environ.get("ARSTRO_TOKEN", "")
size = (sys.argv[3] if len(sys.argv) > 3 else "1280x900").split("x")
failed = 0
os.makedirs(outdir, exist_ok=True)
port = 9333
prof = tempfile.mkdtemp()
proc = subprocess.Popen(["chromium", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=%d" % port,
                         "--user-data-dir=" + prof, "--window-size=%s,%s" % tuple(size), "--window-position=9000,9000",
                         "--no-first-run", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                         "--disable-backgrounding-occluded-windows", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    targets = []
    for _ in range(100):
        try:
            targets = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port))
            if any(t["type"] == "page" for t in targets):
                break
        except OSError:
            pass
        time.sleep(0.2)
    page = next(t for t in targets if t["type"] == "page")
    c = ws.connect(page["webSocketDebuggerUrl"], {})
    nid = [0]
    errors = []

    def call(method, **params):
        nid[0] += 1
        c.send_text(json.dumps({"id": nid[0], "method": method, "params": params}))
        while True:
            op, data = c.recv_message()
            msg = json.loads(data)
            if msg.get("id") == nid[0]:
                return msg.get("result", {})
            handle(msg)

    def handle(msg):
        m = msg.get("method")
        if m == "Runtime.exceptionThrown":
            d = msg["params"]["exceptionDetails"]
            errors.append("EXC %s %s" % (d.get("text"), (d.get("exception") or {}).get("description", "")[:300]))
        elif m == "Runtime.consoleAPICalled" and msg["params"]["type"] in ("error", "warning"):
            errors.append("CONSOLE %s" % " ".join(str(a.get("value") or a.get("description")) for a in msg["params"]["args"])[:300])
        elif m == "Log.entryAdded" and msg["params"]["entry"]["level"] == "error":
            errors.append("LOG %s %s" % (msg["params"]["entry"]["text"][:200], msg["params"]["entry"].get("url", "")))

    def pump(seconds):
        end = time.time() + seconds
        c.sock.settimeout(0.2)
        while time.time() < end:
            try:
                op, data = c.recv_message()
                handle(json.loads(data))
            except (TimeoutError, OSError):
                pass
            except ws.WSClosed:
                break
        c.sock.settimeout(None)

    call("Runtime.enable")
    call("Log.enable")
    call("Page.enable")
    call("Emulation.setDeviceMetricsOverride", width=int(size[0]), height=int(size[1]), deviceScaleFactor=1, mobile=int(size[0]) < 700)
    call("Page.navigate", url=base + "/?token=" + password)
    pump(4)
    routes = sys.argv[4].split(",") if len(sys.argv) > 4 else [
        "monitor", "camera/recorder", "camera/gallery", "screen/screen", "screen/remote", "terminal",
        "connection/wifi", "connection/network", "connection/bluetooth",
        "io/pins", "io/i2c", "io/spi", "io/uart", "io/pwm", "io/adc", "files", "system/system", "system/logs"]
    for r in routes:
        before = len(errors)
        call("Runtime.evaluate", expression="location.hash = '#/%s'" % r)
        pump(3.5)
        res = call("Runtime.evaluate", expression="JSON.stringify({view: document.querySelector('.view') && document.querySelector('.view').dataset.view, "
                                                  "w: document.documentElement.scrollWidth, text: (document.querySelector('.content')||document.body).innerText.slice(0,160)})",
                   returnByValue=True)
        info = json.loads(res["result"]["value"])
        shot = call("Page.captureScreenshot", format="png")
        with open(os.path.join(outdir, r.replace("/", "_") + ".png"), "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        overflow = info["w"] > int(size[0]) + 1
        print("%-22s view=%-10s %s%s | %s" % (r, info["view"], "OVERFLOW %d " % info["w"] if overflow else "",
                                             ("%d errors" % (len(errors) - before)) if len(errors) > before else "ok",
                                             info["text"].replace("\n", " / ")[:110]))
        for e in errors[before:]:
            print("      ", e)
        if overflow or len(errors) > before or info["view"] is None:
            failed += 1
finally:
    proc.kill()
    shutil.rmtree(prof, ignore_errors=True)
print("%s: %d page(s) with problems" % ("FAIL" if failed else "PASS", failed))
sys.exit(1 if failed else 0)
