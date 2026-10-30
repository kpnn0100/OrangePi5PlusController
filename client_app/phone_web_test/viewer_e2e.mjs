#!/usr/bin/env node
// The phone's web UI through the REAL device manager, end to end (PHN-17: "opens from the device
// manager in any browser, works over the HTTP transport"):
//
//   relay (python3 -m arstro_relay, on localhost) ← relay_agent.py (Arstro's Python agent, forwarding
//   to the mock phone's port) ← mock_phone.py --prefix /  …  Chrome: device list → access key →
//   open the port → B/d/<id>/<port>/ through the service worker, the shim's WebSockets and the
//   end-to-end channel - once with WebSockets, once with HTTP pipes (PROTOCOL.md section 6).
//
//   node client_app/phone_web_test/viewer_e2e.mjs [--transport ws|http] [--shots DIR]

import fs from "node:fs";
import os from "node:os";
import net from "node:net";
import path from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const require = createRequire(path.join(REPO, "server", "tests", "web", "package.json"));
const puppeteer = require("puppeteer-core");
const args = process.argv.slice(2);
const opt = (n) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : undefined; };
const SHOTS = opt("--shots") || path.join(HERE, "shots");
const TRANSPORTS = opt("--transport") ? [opt("--transport")] : ["ws", "http"];
const TOKEN = "phone-web-test-join-token-0123456789abcdef";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function check(cond, msg) { if (!cond) throw new Error(msg); }
async function until(fn, timeout = 10000, every = 150) {
  const end = Date.now() + timeout;
  for (;;) {
    let v;
    try { v = await fn(); } catch (e) { v = null; }
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(every);
  }
}
const freePort = () => new Promise((resolve) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); }); });

const procs = [];
function run(cmd, argv, opts, ready) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, argv, { stdio: ["ignore", "pipe", "pipe"], ...opts });
    procs.push(p);
    let out = "";
    const t = setTimeout(() => reject(new Error(cmd + " did not start: " + out.slice(-800))), 60000);
    const on = (d) => { out += d; const m = ready(out); if (m) { clearTimeout(t); resolve(m); } };
    p.stdout.on("data", on);
    p.stderr.on("data", (d) => { out += d; if (process.env.E2E_VERBOSE) process.stderr.write(d); on(""); });
    p.on("exit", (c) => reject(new Error(cmd + " exited " + c + ": " + out.slice(-800))));
  });
}

async function main() {
  const exe = [process.env.CHROME, "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"].find((p) => p && fs.existsSync(p));
  fs.mkdirSync(SHOTS, { recursive: true });
  const data = fs.mkdtempSync(path.join(os.tmpdir(), "phone-relay-"));
  const rport = await freePort();
  const BASE = `http://127.0.0.1:${rport}/arstro/internal/devices`;
  // the device manager
  await run("python3", ["-m", "arstro_relay"], {
    cwd: path.join(REPO, "relay"),
    env: { ...process.env, PYTHONPATH: path.join(REPO, "relay"), RELAY_HOST: "127.0.0.1", RELAY_PORT: String(rport),
           RELAY_DATA: data, RELAY_JOIN_TOKEN: TOKEN },
  }, (o) => /device manager .* on http/.test(o));
  check(await until(() => fetch(BASE + "/").then((r) => r.ok), 20000), "the device manager did not start");
  // the phone (served at the root, as the phone's Router does) and its agent
  const mockUrl = await run("python3", [path.join(HERE, "mock_phone.py"), "--port", "0", "--prefix", "/"], {}, (o) => (o.match(/READY (\S+)/) || [])[1]);
  const MP = Number(new URL(mockUrl).port);
  const [, ID, KEY] = await run("python3", [path.join(HERE, "relay_agent.py"), "--relay", BASE, "--token", TOKEN, "--port", String(MP)],
                                { env: { ...process.env, PYTHONPATH: path.join(REPO, "server") } }, (o) => o.match(/AGENT (\S+) (\S+)/));
  const DEVICE_PAGE = `${BASE}/d/${ID}/${MP}/`;
  const mockLog = () => fetch(`http://127.0.0.1:${MP}/mock/log`).then((r) => r.json());
  const ops = async (n) => ((await mockLog()).ops || []).filter((o) => o.op === n);
  console.log(`device manager ${BASE}/, phone ${ID} → mock :${MP}`);

  const browser = await puppeteer.launch({
    executablePath: exe, headless: true,
    args: ["--no-sandbox", "--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
           "--use-fake-device-for-media-stream", "--mute-audio"],
  });
  let passed = 0, failed = 0;
  try {
    for (const transport of TRANSPORTS) {
      const t0 = Date.now();
      const errors = [];
      await fetch(`http://127.0.0.1:${MP}/mock/clear`, { method: "POST", body: "{}" });
      const ctx = await browser.createBrowserContext();
      let phoneRef = null;
      try {
        // device list → key → open the port
        const page = await ctx.newPage();
        page.on("pageerror", (e) => errors.push(e.message));
        await page.evaluateOnNewDocument((t) => { try { sessionStorage.setItem("arstro-relay-transport", t); } catch (e) { /* opaque */ } }, transport);
        await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
        await page.waitForSelector(`.device[data-id="${ID}"]`, { timeout: 20000 });
        await page.click(`.device[data-id="${ID}"]`);
        await page.waitForSelector("#key", { timeout: 8000 });
        await page.type("#key", KEY);
        await page.click("#check");
        check(await until(() => page.$(`.port-btn[data-port="${MP}"]`), 20000), "the phone's port was not listed");
        check(await until(() => page.$("#key-stored"), 8000), "the key was not stored");
        // the stored key lets the device manager's service worker open B/d/<id>/<port>/ (what "Open"
        // does in a new window; headless Chrome leaves that window on about:blank, so go there here)
        const phone = await ctx.newPage();
        phoneRef = phone;
        phone.on("console", (m) => { if (process.env.E2E_VERBOSE) console.log("  phone console:", m.type(), m.text()); });
        await phone.evaluateOnNewDocument((t) => { try { sessionStorage.setItem("arstro-relay-transport", t); } catch (e) { /* opaque */ } }, transport);
        // (the device manager's boot page may take over the first navigation while its worker starts)
        await phone.goto(DEVICE_PAGE, { waitUntil: "domcontentloaded" }).catch((e) => { if (!/ERR_ABORTED/.test(e.message)) throw e; });
        phone.on("pageerror", (e) => errors.push("phone: " + e.message));
        phone.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d|openstreetmap|ERR_/.test(m.text())) errors.push("phone console: " + m.text()); });
        await phone.setViewport({ width: 1280, height: 860 });
        await phone.waitForSelector(".nav-item", { timeout: 30000 });
        const viewer = await phone.evaluate(() => ({ shim: !!window.__arstroShim, sw: !!(navigator.serviceWorker && navigator.serviceWorker.controller) }));
        check(viewer.shim && viewer.sw, "the viewer is not active in the phone's page: " + JSON.stringify(viewer));
        check(await until(() => phone.$eval(".side .batt", (e) => e.textContent.includes("76%")), 15000), "status topic over the shim's /ws");
        check(await until(() => phone.$eval(".side .conn", (e) => e.textContent === "Online"), 5000), "not online");
        const go = async (v) => { await phone.evaluate((x) => { location.hash = "#/" + x; }, v); await phone.waitForSelector(`.view[data-view="${v}"]`); await sleep(300); };

        // the screen: H.264 through the channel, a tap back
        await go("screen");
        check(await until(() => phone.evaluate(() => window.phoneDebug.screen && window.phoneDebug.screen.video.shown > 10), 25000), "screen frames through the channel");
        const r = await phone.evaluate(() => window.phoneDebug.screen.video.pictureRect());
        await phone.mouse.click(r.left + r.width * 0.3, r.top + r.height * 0.6);
        const tap = await until(async () => ((await mockLog()).screen || []).filter((m) => m.t === "tap").pop(), 8000);
        check(tap && Math.abs(tap.x - 0.3) < 0.01 && Math.abs(tap.y - 0.6) < 0.01, "tap through the channel: " + JSON.stringify(tap));
        const fps = await phone.evaluate(async () => { const v = window.phoneDebug.screen.video; v.sample(); await new Promise((r) => setTimeout(r, 2000)); return v.sample().fps; });
        await phone.screenshot({ path: path.join(SHOTS, `viewer-${transport}-screen.png`) });

        // a call: microphone frames up, the phone's audio down
        await go("call");
        await phone.evaluate(() => [...document.querySelectorAll(".call-card .btn")].find((b) => b.textContent.includes("Call the phone")).click());
        check(await until(() => phone.$eval(".call-state", (e) => e.textContent === "In call"), 15000), "the call did not become active");
        check(await until(() => phone.evaluate(() => window.phoneDebug.call.sent > 40 && window.phoneDebug.call.got > 20), 15000), "call audio through the channel");
        const cs = (await mockLog()).call;
        check(cs.in_frames > 20 && cs.sizes.every((n) => n === 640), "640-byte frames at the phone: " + JSON.stringify({ n: cs.in_frames }));
        await phone.click('.call-card button[aria-label="Hang up"]');
        check(await until(() => phone.$eval(".call-state", (e) => e.textContent === "Call ended"), 8000), "hang up");

        // files: a real browser download (<a download> → the shim) and an upload (XHR PUT of a File)
        const dir = fs.mkdtempSync(path.join(os.tmpdir(), "phone-dl-"));
        const cdp = await browser.target().createCDPSession();
        const dls = [];
        cdp.on("Browser.downloadProgress", (e) => { if (e.state === "completed") dls.push(e.guid); });
        await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dir, eventsEnabled: true, browserContextId: ctx.id });
        await go("files");
        await phone.waitForSelector('.file-row[data-name="Documents"]', { timeout: 15000 });
        await phone.evaluate(() => document.querySelector('.file-row[data-name="Documents"] .name').click());
        await phone.waitForSelector('.file-row[data-name="notes.txt"] a[download]', { timeout: 15000 });
        await phone.click('.file-row[data-name="notes.txt"] a[download]');
        check(await until(() => dls.length && fs.existsSync(path.join(dir, "notes.txt")), 20000), "the download did not complete: " + fs.readdirSync(dir));
        check(fs.readFileSync(path.join(dir, "notes.txt"), "utf8") === "hello from the phone\n", "downloaded content");
        await cdp.detach().catch(() => {});
        const upName = `big-upload-${transport}.bin`;              // the mock is shared by both runs
        const up = path.join(dir, upName);
        fs.writeFileSync(up, Buffer.alloc(600_000, 3));
        await (await phone.$("input[type=file]")).uploadFile(up);
        check(await until(async () => ((await mockLog()).uploads || []).some((u) => u.name === upName && u.size === 600000), 30000), "upload through the channel");
        check(await until(() => phone.$(`.file-row[data-name="${upName}"]`), 10000), "uploaded file not listed");
        check(await until(async () => !(await phone.$(".upl-row")), 10000), "the upload row did not finish");
        await phone.screenshot({ path: path.join(SHOTS, `viewer-${transport}-files.png`) });
        fs.rmSync(dir, { recursive: true, force: true });

        // messages: an op and a live event
        await go("messages");
        await phone.waitForSelector(".thread-item", { timeout: 15000 });
        check(await until(async () => {
          if (await phone.$(".msg-layout.in-thread .bubble")) return true;
          await phone.click(".thread-item").catch(() => {});
          return false;
        }, 15000, 2000), "the conversation did not open");
        await phone.type(".composer textarea", "via " + transport);
        await phone.keyboard.press("Enter");
        check(await until(async () => (await ops("messages.send")).some((o) => o.args.text === "via " + transport), 10000), "messages.send through the channel");

        // the service worker's one channel to the phone uses the forced transport
        const st = await phone.evaluate(() => new Promise((resolve, reject) => {
          const mc = new MessageChannel();
          const t = setTimeout(() => reject(new Error("no answer")), 5000);
          mc.port1.onmessage = (e) => { clearTimeout(t); resolve(e.data); };
          navigator.serviceWorker.controller.postMessage({ t: "status" }, [mc.port2]);
        }));
        const mine = st.channels.filter((c) => c.id === ID && c.open);
        check(mine.length === 1 && mine[0].transport === transport, "channels: " + JSON.stringify(st.channels));
        check(!errors.length, "page errors: " + errors.join(" | "));
        passed++;
        console.log(`PASS  phone_ui_through_the_device_manager_${transport.padEnd(5)} ${((Date.now() - t0) / 1000).toFixed(1).padStart(5)}s  ` +
                    `PHN-17 PHN-04 PHN-06 PHN-11 PHN-08  (screen ${fps.toFixed(1)} fps)`);
      } catch (e) {
        failed++;
        console.log(`FAIL  phone_ui_through_the_device_manager_${transport}  ${e.message}`);
        if (phoneRef) {
          const where = await phoneRef.evaluate(() => [location.href, document.title, document.body ? document.body.innerText.slice(0, 400) : ""]).catch((x) => [x.message]);
          console.log("      phone page: " + JSON.stringify(where));
          if (errors.length) console.log("      errors: " + errors.join(" | "));
          await phoneRef.screenshot({ path: path.join(SHOTS, `viewer-${transport}-fail.png`) }).catch(() => {});
        }
      } finally {
        await ctx.close().catch(() => {});
      }
    }
  } finally {
    await browser.close();
    for (const p of procs) p.kill();
    fs.rmSync(data, { recursive: true, force: true });
  }
  console.log(`${passed} passed, ${failed} failed`);
  return failed ? 1 : 0;
}

process.exit(await main());
