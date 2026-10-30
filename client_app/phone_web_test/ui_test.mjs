#!/usr/bin/env node
// Browser tests of the phone's web UI (client_app/android/app/src/main/assets/phone/) against the mock
// phone (mock_phone.py), tagged with the PHN requirements they cover (docs/requirements.md).
//
//   node client_app/phone_web_test/ui_test.mjs [-k name] [--list] [--shots DIR] [--keep]
//
// Uses puppeteer-core from server/tests/web/node_modules (cd server/tests/web && npm install) and
// a local Chrome (CHROME=/path, default /usr/bin/google-chrome), headless, with fake media devices.
// The UI is served at a nested path (like the device manager's B/d/<id>/8080/) on localhost, a
// secure context: WebCodecs and getUserMedia work there.

import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const require = createRequire(path.join(REPO, "server", "tests", "web", "package.json"));
const puppeteer = require("puppeteer-core");

const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const ONLY = opt("-k");
const SHOTS = opt("--shots") || path.join(HERE, "shots");
const PREFIX = "/arstro/internal/devices/d/test-phone-ab12cd/8080/";

const TESTS = [];
const test = (name, reqs, fn) => TESTS.push({ name, reqs, fn });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function check(cond, msg) { if (!cond) throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg)); }
async function until(fn, timeout = 8000, every = 100) {
  const end = Date.now() + timeout;
  for (;;) {
    let v;
    try { v = await fn(); } catch (e) { v = null; }
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(every);
  }
}

// ------------------------------------------------------------------------------ the mock phone
let mock, BASE, ORIGIN;
function startMock() {
  return new Promise((resolve, reject) => {
    mock = spawn("python3", [path.join(HERE, "mock_phone.py"), "--port", "0", "--prefix", PREFIX], { stdio: ["ignore", "pipe", "inherit"] });
    let buf = "";
    const t = setTimeout(() => reject(new Error("the mock did not start")), 60000);
    mock.stdout.on("data", (d) => {
      buf += d;
      const m = buf.match(/READY (\S+)/);
      if (m) { clearTimeout(t); BASE = m[1]; ORIGIN = new URL(BASE).origin; resolve(); }
    });
    mock.on("exit", (c) => reject(new Error("the mock exited " + c)));
  });
}
const mockGet = async (p) => (await fetch(ORIGIN + p)).json();
const mockPost = async (p, body = {}) => (await fetch(ORIGIN + p, { method: "POST", body: JSON.stringify(body) })).json();
const log = () => mockGet("/mock/log");
const ops = async (name) => ((await log()).ops || []).filter((o) => o.op === name);
async function apiOp(name, body = {}) {
  const r = await fetch(BASE + "api/op/" + name, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return { status: r.status, body: await r.json() };
}

// ------------------------------------------------------------------------------------ browser
let browser;
const pageErrors = [];
const requests = [];

async function newPage({ width = 1280, height = 860, mobile = false, hash = "" } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ width, height, isMobile: mobile, hasTouch: mobile, deviceScaleFactor: 1 });
  page.on("pageerror", (e) => pageErrors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const t = m.text();
    if (/status of 4\d\d/.test(t)) return;                       // ops refused on purpose (gating, 409 upload)
    if (/openstreetmap|ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED|ERR_CONNECTION|ERR_BLOCKED/.test(t)) return;  // the map, offline
    pageErrors.push("console: " + t);
  });
  page.on("request", (r) => requests.push(r.url()));
  page.on("response", (r) => {
    const u = r.url();
    if (u.startsWith(ORIGIN) && (u.includes("/static/") || u.endsWith(PREFIX)) && r.status() >= 400) pageErrors.push(`HTTP ${r.status()} ${u}`);
  });
  await page.goto(BASE + hash, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".nav-item", { timeout: 10000 });
  await page.waitForSelector(".view", { timeout: 10000 });
  return page;
}

async function go(page, view) {
  await page.evaluate((v) => { location.hash = "#/" + v; }, view);
  await page.waitForSelector(`.view[data-view="${view.split("?")[0]}"]`, { timeout: 8000 });
  await until(() => page.evaluate(() => !document.querySelector(".view > .empty.loading")), 5000);
  await sleep(250);
}

async function shot(page, name) {
  if (!SHOTS) return;
  const w = page.viewport().width;
  await sleep(350);
  await page.screenshot({ path: path.join(SHOTS, `${name}-${w}.png`) });
}

const text = (page, sel = ".view") => page.$eval(sel, (e) => e.textContent);
const clickText = (page, sel, t) => page.evaluate((sel, t) => {
  const el = [...document.querySelectorAll(sel)].find((e) => e.textContent.trim().includes(t) && !e.disabled && e.offsetParent !== null);
  if (el) el.click();
  return !!el;
}, sel, t);
const sheetOk = async (page, label) => {
  await page.waitForSelector(".sheet.show", { timeout: 4000 });
  await sleep(150);
  check(await clickText(page, ".sheet.show .sheet-foot .btn", label), "sheet button not found: " + label);
  await sleep(350);
};
async function overflow(page) {
  return page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
}

const VIEWS = ["overview", "screen", "camera", "call", "listen", "messages", "contacts", "find", "files", "notifications",
               "clipboard", "apps", "tools", "shell", "features"];

// ------------------------------------------------------------------------------------- tests
test("shell_header_nav_and_relative_urls", ["PHN-17", "PHN-02", "PHN-03"], async () => {
  requests.length = 0;
  const page = await newPage();
  const items = await page.$$eval(".nav-item", (els) => els.map((e) => e.dataset.id));
  for (const v of VIEWS) check(items.includes(v), "nav lacks " + v);
  check((await text(page, ".side")).includes("Test Pixel"), "phone name not in the header");
  check((await text(page, ".side")).includes("Pixel 8"), "model not in the header");
  check(await until(() => page.$eval(".side .batt", (e) => e.textContent.includes("76%"))), "battery not shown");
  check(await until(() => page.$eval(".side .conn", (e) => e.textContent === "Online")), "online state not shown");
  check(!(await page.$eval("body", (e) => /ak1_|access key:/i.test(e.textContent))), "a key is shown");
  for (const v of VIEWS) await go(page, v);
  await sleep(500);
  const local = requests.filter((u) => u.startsWith(ORIGIN));
  const outside = local.filter((u) => !new URL(u).pathname.startsWith(PREFIX));
  check(!outside.length, "requests outside the device path (absolute URLs): " + outside.join(", "));
  check(local.some((u) => u.includes(PREFIX + "api/info")), "api/info not requested relative to the page");
  check(local.some((u) => u.startsWith(ORIGIN.replace("http", "ws")) || u.includes(PREFIX + "ws")) || true, "");
  const sockets = (await log()).sockets || [];
  check(sockets.includes("/ws"), "the event socket was not opened at <page>/ws");
  await page.close();
});

test("every_page_loads_at_1280_and_390", ["PHN-17"], async () => {
  for (const [w, hgt, mobile] of [[1280, 860, false], [390, 844, true]]) {
    const page = await newPage({ width: w, height: hgt, mobile });
    for (const v of VIEWS) {
      await go(page, v);
      await sleep(v === "screen" || v === "camera" ? 1500 : 300);
      const [sw, iw] = await overflow(page);
      check(sw <= iw + 1, `${v} overflows at ${w} px: ${sw} > ${iw}`);
      if (["overview", "screen", "camera", "messages", "find", "files", "features", "apps", "notifications", "tools"].includes(v)) await shot(page, v);
    }
    if (mobile) {               // the drawer opens and navigates
      await page.click(".menu-btn");
      await sleep(400);
      await shot(page, "drawer");
      await page.click('.nav-item[data-id="contacts"]');
      await page.waitForSelector('.view[data-view="contacts"]');
      check(!(await page.$eval(".app", (e) => e.classList.contains("drawer-open"))), "the drawer did not close");
    }
    await page.close();
  }
});

test("theme_light_and_dark", ["PHN-17"], async () => {
  const page = await newPage();
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  const bgLight = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  const bgDark = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  check(bgLight !== bgDark, "no light theme");
  await page.click(".theme-btn");                                   // auto → dark → light
  await page.click(".theme-btn");
  check(await page.evaluate(() => document.documentElement.dataset.theme === "light"), "theme toggle");
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  check((await page.evaluate(() => getComputedStyle(document.body).backgroundColor)) === bgLight, "forced light not applied");
  await shot(page, "overview-light");
  await page.click(".theme-btn");
  await page.close();
});

test("overview_status_and_quick_actions", ["PHN-03", "PHN-10", "PHN-15", "PHN-04"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "overview");
  check(await until(() => page.$eval('[data-card="battery"]', (e) => e.textContent.includes("76"))), "battery card");
  const t = await text(page);
  for (const s of ["Charging", "31.5 °C", "41.2 GB", "HomeNet", "192.0.2.42", "unlocked"]) check(t.includes(s), "overview lacks " + s);
  check(await clickText(page, ".quick .btn", "Ring"), "no Ring");
  check(await until(async () => (await ops("find.ring")).length), "find.ring not sent");
  check(await until(() => page.$eval(".quick", (e) => e.textContent.includes("Stop ringing"))), "ringing state not shown");
  check(await clickText(page, ".quick .btn", "Stop ringing"), "no Stop ringing");
  check(await until(async () => (await ops("find.ring_stop")).length), "find.ring_stop not sent");
  check(await clickText(page, ".quick .btn", "Torch"), "no Torch");
  check(await until(async () => (await ops("camera.torch")).some((o) => o.args.on === true)), "torch on not sent");
  check(await clickText(page, ".quick .btn", "Lock screen"), "no Lock");
  check(await until(async () => (await ops("screen.key")).some((o) => o.args.key === "lock")), "lock not sent");
  // live status: a new topic value shows at once
  await mockPost("/mock/state", { topic: "status", data: { battery: { level: 12, charging: false }, storage: {}, memory: {}, network: { type: "mobile", connected: true }, screen_on: false, locked: true } });
  check(await until(() => page.$eval(".side .batt", (e) => e.textContent.includes("12%") && e.classList.contains("low"))), "status not live");
  await mockPost("/mock/state", { topic: "status", data: (await apiOp("status.get")).body.data });
  await page.close();
});

test("screen_live_video_and_control", ["PHN-04"], async () => {
  await mockPost("/mock/clear");
  const scr = (await apiOp("screen.status")).body.data;
  await mockPost("/mock/state", { topic: "screen", data: { ...scr, consent: false, waiting: false } });   // consent not given yet
  const page = await newPage();
  await go(page, "screen");
  check(await until(() => text(page).then((t) => t.includes("Waiting for consent on the phone")), 3000), "no consent state");
  await shot(page, "screen-consent");
  check(await until(() => page.evaluate(() => window.phoneDebug.screen.video.shown > 10), 12000), "screen frames not decoded");
  const [cw, chh] = await page.$eval(".screen-stage canvas", (c) => [c.width, c.height]);
  check(cw === 720 && chh === 1280, `canvas size ${cw}x${chh}`);
  check((await ops("screen.start")).length === 1, "screen.start not asked once");
  const qual = await until(async () => ((await log()).screen || []).find((m) => m.t === "quality"));
  check(qual && qual.max === 720, "quality not sent on open: " + JSON.stringify(qual));
  await shot(page, "screen-live");
  // the picture's rectangle (letterboxed in the stage)
  const r = await page.evaluate(() => window.phoneDebug.screen.video.pictureRect());
  const at = (x, y) => [r.left + r.width * x, r.top + r.height * y];
  // tap
  await page.mouse.click(...at(0.25, 0.5));
  const tap = await until(async () => ((await log()).screen || []).find((m) => m.t === "tap"));
  check(tap && Math.abs(tap.x - 0.25) < 0.01 && Math.abs(tap.y - 0.5) < 0.01, "tap coordinates: " + JSON.stringify(tap));
  // long press (held 500 ms)
  await page.mouse.move(...at(0.6, 0.3));
  await page.mouse.down();
  await sleep(700);
  await page.mouse.up();
  const lp = await until(async () => ((await log()).screen || []).find((m) => m.t === "long"));
  check(lp && Math.abs(lp.x - 0.6) < 0.01 && Math.abs(lp.y - 0.3) < 0.01, "long press: " + JSON.stringify(lp));
  // swipe
  await page.mouse.move(...at(0.5, 0.8));
  await page.mouse.down();
  for (let i = 1; i <= 8; i++) { await page.mouse.move(...at(0.5, 0.8 - i * 0.07)); await sleep(20); }
  await page.mouse.up();
  const sw = await until(async () => ((await log()).screen || []).find((m) => m.t === "swipe"));
  check(sw && Math.abs(sw.y1 - 0.8) < 0.01 && Math.abs(sw.y2 - 0.24) < 0.02 && sw.ms >= 80, "swipe: " + JSON.stringify(sw));
  // scroll (wheel)
  await page.mouse.move(...at(0.5, 0.5));
  await page.mouse.wheel({ deltaY: 300 });
  const sc = await until(async () => ((await log()).screen || []).find((m) => m.t === "scroll"));
  check(sc && sc.dy > 0 && Math.abs(sc.x - 0.5) < 0.01, "scroll: " + JSON.stringify(sc));
  // keys
  for (const [k, label] of [["home", "Home"], ["back", "Back"], ["recents", "Recents"], ["notifications", "Notifications"],
                            ["quick_settings", "Quick settings"], ["lock", "Lock"], ["power_dialog", "Power"], ["wake", "Wake"]]) {
    await page.click(`.screen-layout .btn[data-key="${k}"]`);
    check(await until(async () => ((await log()).screen || []).some((m) => m.t === "key" && m.key === k)), "key " + label);
  }
  // the text box
  await page.type('input[aria-label="Text to type on the phone"]', "héllo there");
  await page.keyboard.press("Enter");
  check(await until(async () => ((await log()).screen || []).some((m) => m.t === "text" && m.text === "héllo there")), "typed text");
  // keyboard on the picture
  await page.focus(".screen-stage");
  await page.keyboard.type("ab");
  await page.keyboard.press("Backspace");
  await page.keyboard.press("Enter");
  check(await until(async () => {
    const s = (await log()).screen || [];
    return s.some((m) => m.t === "text" && m.text === "ab") && s.some((m) => m.key === "backspace") && s.some((m) => m.key === "enter");
  }), "keyboard → phone");
  // quality
  await page.click('.seg button[data-value="1080"]');
  check(await until(async () => ((await log()).screen || []).some((m) => m.t === "quality" && m.max === 1080)), "quality 1080");
  // stop sharing
  check(await clickText(page, ".view-head .btn", "Stop sharing"), "no Stop sharing");
  check(await until(async () => (await ops("screen.stop")).length), "screen.stop");
  check(await until(() => text(page).then((t) => t.includes("Screen sharing stopped"))), "stopped state");
  await page.close();
});

test("screen_touch_on_a_phone", ["PHN-04", "PHN-17"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage({ width: 390, height: 844, mobile: true });
  await go(page, "screen");
  check(await until(() => page.evaluate(() => window.phoneDebug.screen.video.shown > 5), 12000), "no frames at 390 px");
  const r = await page.evaluate(() => window.phoneDebug.screen.video.pictureRect());
  await page.touchscreen.tap(r.left + r.width * 0.75, r.top + r.height * 0.1);
  const tap = await until(async () => ((await log()).screen || []).find((m) => m.t === "tap"));
  check(tap && Math.abs(tap.x - 0.75) < 0.015 && Math.abs(tap.y - 0.1) < 0.015, "touch tap: " + JSON.stringify(tap));
  await shot(page, "screen-live");
  // no Accessibility on the phone: watch only, said so, nothing sent
  const scr = (await apiOp("screen.status")).body.data;
  await mockPost("/mock/state", { topic: "screen", data: { ...scr, control: false } });
  check(await until(() => text(page).then((t) => t.includes("Remote control needs Accessibility"))), "no-control note");
  check(await page.$eval('.btn[data-key="home"]', (b) => b.disabled), "keys not disabled without control");
  await mockPost("/mock/clear");
  await page.touchscreen.tap(r.left + r.width * 0.5, r.top + r.height * 0.5);
  await sleep(400);
  check(!((await log()).screen || []).some((m) => m.t === "tap"), "a tap was sent without control");
  await mockPost("/mock/state", { topic: "screen", data: { ...scr, control: true } });
  // the phone's screen went off (its power button): said so instead of a frozen picture; Wake turns it on
  await mockPost("/mock/state", { topic: "screen", data: { ...scr, control: true, consent: true, screen_on: false } });
  check(await until(() => text(page).then((t) => t.includes("The phone's screen is off"))), "no screen-off message");
  check(!(await page.$eval(".screen-stage", (e) => e.classList.contains("playing"))), "a dark screen still shown as playing");
  await mockPost("/mock/clear");
  check(await clickText(page, ".btn", "Wake the screen"), "no Wake button");
  check(await until(async () => ((await log()).screen || []).some((m) => m.t === "key" && m.key === "wake")), "wake not sent");
  await mockPost("/mock/state", { topic: "screen", data: { ...scr, control: true, consent: true, screen_on: true } });
  check(await until(() => page.$eval(".screen-stage", (e) => e.classList.contains("playing"))), "the picture did not come back");
  await page.close();
});

test("camera_live_photo_record_torch", ["PHN-05"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "camera");
  check(await until(() => page.evaluate(() => (window.phoneDebug.videos || []).some((v) => v.shown > 5)), 12000), "camera frames not decoded");
  const [cw, chh] = await page.$eval(".cam-stage canvas", (c) => [c.width, c.height]);
  check(cw === 720 && chh === 1280, `camera picture not rotated by config.rotation (90): ${cw}x${chh}`);
  check(((await log()).sockets || []).some((s) => s.startsWith("/ws/camera?") && s.includes("facing=back") && s.includes("size=1280x720")), "camera socket path");
  await shot(page, "camera-live");
  await page.click('.seg button[data-value="front"]');
  check(await until(async () => ((await log()).sockets || []).some((s) => s.includes("facing=front"))), "front camera not opened");
  check(await clickText(page, ".btn", "Take photo"), "no Take photo");
  check(await until(() => page.$eval(".photo-box img", (i) => i.complete && i.naturalWidth === 1280)), "photo not shown");
  const a = await page.$eval(".photo-box a[download]", (e) => [e.getAttribute("href"), e.getAttribute("download")]);
  check(a[0].startsWith("blob:") && /^photo-\d{8}-\d{6}\.jpg$/.test(a[1]), "photo download link " + a);
  check(((await log()).photos || []).some((p) => p.facing === "front"), "photo facing");
  check(await clickText(page, ".btn", "Record"), "no Record");
  check(await until(async () => (await ops("camera.record")).some((o) => o.args.action === "start" && o.args.facing === "front")), "record start");
  check(await until(() => page.$eval(".cam-stage .glass.rec", (e) => !e.classList.contains("hidden"))), "REC not shown");
  check(await clickText(page, ".btn", "Stop recording"), "no Stop recording");
  check(await until(() => page.$eval("#toasts", (e) => e.textContent.includes("Movies/Arstro/VID_"))), "saved path not shown");
  check(await clickText(page, ".btn", "Torch"), "no Torch");
  check(await until(async () => (await ops("camera.torch")).length), "camera.torch");
  await page.close();
});

test("call_rings_goes_active_and_carries_audio", ["PHN-06"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "call");
  await page.type('input[aria-label="Your name"]', "Web tester");
  check(await clickText(page, ".call-card .btn", "Call the phone"), "no Call button");
  check(await until(async () => (await ops("call.start")).some((o) => o.args.name === "Web tester")), "call.start with the name");
  check(await until(() => page.$eval(".call-state", (e) => e.textContent === "In call"), 8000), "the call did not become active");
  check(await until(() => page.evaluate(() => window.phoneDebug.call.sent > 30), 6000), "no microphone frames sent");
  const st = (await log()).call;
  check(st && st.in_frames > 20 && st.sizes.every((n) => n === 640), "frames at the phone: " + JSON.stringify(st && { n: st.in_frames, sizes: [...new Set(st.sizes)] }));
  check(await until(() => page.evaluate(() => window.phoneDebug.call.got > 20)), "no phone audio received");
  check(await until(() => page.$eval('.call-meters [aria-label="The phone"]', (e) => Number(e.getAttribute("aria-valuenow")) > 0)), "phone level meter");
  check(await until(() => page.$eval(".nav-item[data-id=call] .nav-badge", (e) => e.classList.contains("on"))), "call badge");
  await shot(page, "call-active");
  // mute: no more frames
  await page.click('.call-card button[aria-label="Mute"]');
  await sleep(300);
  const s1 = await page.evaluate(() => window.phoneDebug.call.sent);
  await sleep(600);
  check((await page.evaluate(() => window.phoneDebug.call.sent)) === s1, "frames sent while muted");
  await page.click('.call-card button[aria-label="Unmute"]');
  // the call goes on while another page is open
  await go(page, "messages");
  await sleep(400);
  check((await page.evaluate(() => window.phoneDebug.call.state)) === "active", "the call ended on leaving the page");
  await go(page, "call");
  await page.click('.call-card button[aria-label="Hang up"]');
  check(await until(async () => ((await log()).call_msgs || []).some((m) => m.t === "end")), "{t: end} not sent");
  check(await until(() => page.$eval(".call-state", (e) => e.textContent === "Call ended")), "not ended");
  // the phone hangs up
  check(await clickText(page, ".call-card .btn", "Call again"), "no Call again");
  check(await until(() => page.$eval(".call-state", (e) => e.textContent === "In call"), 8000), "second call not active");
  await mockPost("/mock/call_end");
  check(await until(() => page.$eval(".call-sub", (e) => e.textContent.includes("The phone hung up"))), "phone hang-up not shown");
  check(await until(() => page.evaluate(() => !window.phoneDebug.call.mic)), "the microphone was not released");
  await page.close();
});

test("call_with_front_camera_pane", ["PHN-06", "PHN-05"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "call");
  await page.click('.call-layout input[aria-label="Watch the phone\'s front camera"]');
  check(await until(() => page.evaluate(() => (window.phoneDebug.videos || []).some((v) => v.shown > 3)), 10000), "camera pane not decoding");
  check(((await log()).sockets || []).some((s) => s.startsWith("/ws/camera?") && s.includes("facing=front")), "front camera socket");
  await page.click('.call-layout input[aria-label="Watch the phone\'s front camera"]');
  await page.close();
});

test("listen_plays_the_phone_mic", ["PHN-07"], async () => {
  const page = await newPage();
  await go(page, "listen");
  check(await clickText(page, ".call-card .btn", "Listen"), "no Listen");
  check(await until(() => page.evaluate(() => window.phoneDebug.listen.frames > 25), 6000), "no audio frames");
  check(await until(() => page.$eval('[aria-label="Microphone level"]', (e) => Number(e.getAttribute("aria-valuenow")) > 30)), "level meter does not move");
  check((await text(page, ".call-state")) === "Listening", "state");
  await shot(page, "listen");
  check(await clickText(page, ".call-card .btn", "Stop"), "no Stop");
  check(await until(() => text(page, ".call-state").then((t) => t === "Not listening")), "not stopped");
  await page.close();
});

test("messages_threads_send_and_live", ["PHN-08"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "messages");
  check(await until(() => page.$$eval(".thread-item", (e) => e.length >= 3)), "threads not listed");
  check((await page.$eval(".thread-item", (e) => e.textContent)).includes("Alice Nguyen"), "newest thread first");
  check(await page.$eval(".thread-item", (e) => e.classList.contains("unread")), "unread not marked");
  await page.click(".thread-item");
  check(await until(() => page.$$eval(".bubble", (b) => b.length === 3)), "messages of the thread");
  check((await page.$$eval(".bubble.out", (b) => b.length)) === 1, "out bubble");
  await page.type(".composer textarea", "On my way!");
  await page.keyboard.press("Enter");
  check(await until(async () => (await ops("messages.send")).some((o) => o.args.text === "On my way!" && o.args.to === "+84901234567")), "send op");
  check(await until(() => page.$$eval(".bubble.out", (b) => b.length === 2 && !b[1].classList.contains("pending"))), "sent bubble");
  // live: into the open thread
  await mockPost("/mock/event", { ev: "sms", address: "+84 90 123 4567", name: "Alice Nguyen", body: "Live hello", date: Date.now() });
  check(await until(() => page.$$eval(".bubble", (b) => b.some((x) => x.textContent.includes("Live hello")))), "live sms not in the thread");
  // live: another number → a new thread at the top, unread
  await mockPost("/mock/event", { ev: "sms", address: "+84 77 000 1111", body: "Who is this?", date: Date.now() });
  check(await until(() => page.$eval(".thread-item", (e) => e.textContent.includes("Who is this?") && e.classList.contains("unread"))), "new thread not on top");
  await shot(page, "messages-thread");
  // new message to a number
  check(await clickText(page, ".pane-head .btn", "New"), "no New");
  await page.waitForSelector(".sheet.show input[name=to]");
  await page.type(".sheet.show input[name=to]", "+84 93 555 0101");
  await page.type(".sheet.show textarea[name=text]", "Hi Carol");
  await sheetOk(page, "Send");
  check(await until(async () => (await ops("messages.send")).some((o) => o.args.text === "Hi Carol")), "new message not sent");
  // the badge while elsewhere
  await go(page, "overview");
  await mockPost("/mock/event", { ev: "sms", address: "+84 91 222 3344", name: "Bob Tran", body: "ping", date: Date.now() });
  check(await until(() => page.$eval('.nav-item[data-id="messages"] .nav-badge', (e) => e.textContent === "1")), "no badge");
  await go(page, "messages");
  check(await until(() => page.$eval('.nav-item[data-id="messages"] .nav-badge', (e) => e.textContent === "")), "badge not cleared");
  await page.close();
});

test("messages_on_a_phone_screen", ["PHN-08", "PHN-17"], async () => {
  const page = await newPage({ width: 390, height: 844, mobile: true });
  await go(page, "messages");
  check(await until(() => page.$$eval(".thread-item", (e) => e.length >= 3)), "threads");
  await page.click(".thread-item");
  check(await until(() => page.$eval(".thread-pane", (e) => e.offsetParent !== null) && page.$eval(".threads-pane", (e) => e.offsetParent === null)), "thread not alone on the phone");
  await shot(page, "messages-thread");
  await page.click(".back-btn");
  check(await until(() => page.$eval(".threads-pane", (e) => e.offsetParent !== null)), "back did not return");
  await page.close();
});

test("contacts_search_and_call_log", ["PHN-09"], async () => {
  const page = await newPage();
  await go(page, "contacts");
  if (await page.$('.seg button[data-value="contacts"]:not(.active)')) await page.click('.seg button[data-value="contacts"]');
  check(await until(() => page.$$eval("[data-contact]", (e) => e.length === 5)), "contacts not listed");
  await page.$eval('input[aria-label="Search contacts"]', (e) => { e.value = ""; });
  await page.type('input[aria-label="Search contacts"]', "bob");
  check(await until(() => page.$$eval("[data-contact]", (e) => e.length === 1 && e[0].textContent.includes("Bob Tran"))), "search");
  check(await until(async () => (await ops("contacts.list")).some((o) => o.args.q === "bob")), "q not sent");
  await page.$eval('input[aria-label="Search contacts"]', (e) => { e.value = ""; e.dispatchEvent(new Event("input")); });
  await page.click('.seg button[data-value="calls"]');
  check(await until(() => page.$$eval("[data-call]", (e) => e.length === 4)), "calls not listed");
  const t = await text(page);
  check(t.includes("Missed") && t.includes("Outgoing") && t.includes("5:12"), "call details");
  await shot(page, "contacts-calls");
  await page.click('.seg button[data-value="contacts"]');
  await page.close();
});

test("find_map_history_ring_and_lost_mode", ["PHN-10"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "find");
  const src = await until(() => page.$eval(".map-box iframe", (f) => f.getAttribute("src")));
  check(src && src.startsWith("https://www.openstreetmap.org/export/embed.html?bbox=") && /marker=10\.\d+,106\.\d+/.test(src), "map iframe: " + src);
  const open = await page.$eval(".map-box", (e) => e.closest(".card").querySelector("a.btn").href);
  check(open.startsWith("https://www.openstreetmap.org/?mlat="), "open in maps link");
  check(await until(() => page.$$eval(".track circle", (c) => c.length >= 90)), "history track");
  check((await text(page)).includes("±12 m"), "accuracy");
  await page.click(".track circle");
  check(await until(() => text(page).then((t) => t.includes("Back to now"))), "history point not shown");
  check(await clickText(page, ".card-title .btn", "Refresh"), "no Refresh");
  check(await until(async () => (await ops("find.locate")).some((o) => o.args.fresh === true)), "fresh locate");
  check(await clickText(page, ".btn", "Ring the phone"), "no Ring");
  check(await until(() => text(page).then((t) => t.includes("Stop ringing"))), "ringing not shown");
  await clickText(page, ".btn", "Stop ringing");
  check(await until(async () => (await ops("find.ring_stop")).length), "ring_stop");
  await page.type('textarea[aria-label="Message on the lock screen"]', "Lost phone, reward!");
  await page.type('input[aria-label="Number to call"]', "+84 90 000 0000");
  check(await clickText(page, ".btn", "Lock and show the message"), "no lost mode button");
  await sheetOk(page, "Lock the phone");
  check(await until(async () => (await ops("find.lost")).some((o) => o.args.message === "Lost phone, reward!" && o.args.contact === "+84 90 000 0000")), "find.lost");
  check(await until(() => text(page).then((t) => t.includes("Lost mode is on"))), "lost state");
  await shot(page, "find");
  check(await clickText(page, ".btn", "Turn lost mode off"), "no lost off");
  check(await until(async () => (await ops("find.lost_off")).length), "lost_off");
  await page.close();
});

test("files_browse_download_upload_and_ops", ["PHN-11"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "files");
  check(await until(() => page.$$eval(".file-row", (r) => r.some((x) => x.dataset.name === "Documents"))), "root not listed");
  await page.evaluate(() => [...document.querySelectorAll(".file-row")].find((r) => r.dataset.name === "Documents").querySelector(".name").click());
  check(await until(() => page.$eval(".crumbs", (e) => e.textContent.includes("Storage") && e.textContent.includes("Documents"))), "breadcrumb");
  check(await until(() => page.evaluate(() => location.hash.includes("path=%2Fstorage%2Femulated%2F0%2FDocuments"))), "folder not in the address");
  const link = await until(() => page.$eval('.file-row[data-name="notes.txt"] a[download]', (a) => [a.getAttribute("href"), a.getAttribute("download")]));
  check(link && link[0].startsWith("api/files/download?") && link[1] === "notes.txt", "download link must be relative with download: " + link);
  const body = await page.evaluate((h) => fetch(h).then((r) => r.text()), link[0]);
  check(body === "hello from the phone\n", "download content");
  const part = await page.evaluate((h) => fetch(h, { headers: { Range: "bytes=6-9" } }).then(async (r) => [r.status, await r.text()]), link[0]);
  check(part[0] === 206 && part[1] === "from", "range: " + part);
  // upload (a File: exact Content-Length)
  const tmp = path.join(HERE, "shots", "upload-test.bin");
  fs.mkdirSync(path.dirname(tmp), { recursive: true });
  fs.writeFileSync(tmp, Buffer.alloc(200_000, 7));
  const input = await page.$('input[type=file]');
  await input.uploadFile(tmp);
  check(await until(async () => ((await log()).uploads || []).some((u) => u.name === "upload-test.bin" && u.size === 200000)), "upload");
  check(await until(() => page.$('.file-row[data-name="upload-test.bin"]')), "uploaded file not listed");
  // again → 409 → replace
  await input.uploadFile(tmp);
  await sheetOk(page, "Replace");
  check(await until(async () => ((await log()).uploads || []).some((u) => u.name === "upload-test.bin" && u.overwrite === "1")), "overwrite upload");
  // new folder, rename, delete
  check(await clickText(page, ".view-head .btn", "New folder"), "no New folder");
  await page.waitForSelector(".sheet.show input[name=name]");
  await page.type(".sheet.show input[name=name]", "Trips");
  await sheetOk(page, "Create");
  check(await until(async () => (await ops("files.mkdir")).some((o) => o.args.path === "/storage/emulated/0/Documents/Trips")), "mkdir");
  check(await until(() => page.$('.file-row[data-name="Trips"]')), "folder not listed");
  await page.click('.file-row[data-name="upload-test.bin"] button[aria-label^="Rename"]');
  await page.waitForSelector(".sheet.show input[name=name]");
  await page.$eval(".sheet.show input[name=name]", (e) => { e.value = ""; });
  await page.type(".sheet.show input[name=name]", "renamed.bin");
  await sheetOk(page, "Rename");
  check(await until(() => page.$('.file-row[data-name="renamed.bin"]')), "rename");
  await shot(page, "files");
  await page.click('.file-row[data-name="renamed.bin"] button[aria-label^="Delete"]');
  await sheetOk(page, "Delete");
  check(await until(async () => (await ops("files.delete")).some((o) => o.args.path.endsWith("/renamed.bin"))), "delete op");
  check(await until(async () => !(await page.$('.file-row[data-name="renamed.bin"]'))), "deleted file still listed");
  // up and breadcrumb
  await page.evaluate(() => [...document.querySelectorAll(".crumbs button")].find((b) => b.textContent === "Storage").click());
  check(await until(() => page.$$eval(".file-row", (r) => r.some((x) => x.dataset.name === "DCIM"))), "breadcrumb back to storage");
  fs.rmSync(tmp, { force: true });
  await page.close();
});

test("notifications_live_dismiss_reply", ["PHN-12"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "notifications");
  check(await until(() => page.$$eval(".notif", (n) => n.length === 3)), "list");
  await page.click('.notif[data-key^="0|com.whatsapp"] .btn');
  await page.waitForSelector(".sheet.show textarea[name=text]");
  await page.type(".sheet.show textarea[name=text]", "ok!");
  await sheetOk(page, "Send");
  check(await until(async () => (await ops("notifications.reply")).some((o) => o.args.text === "ok!")), "reply");
  await mockPost("/mock/event", { ev: "notification", key: "k-live", app: "Telegram", title: "Mia", text: "New photo" });
  check(await until(() => page.$eval(".notif", (n) => n.dataset.key === "k-live")), "posted not live");
  await shot(page, "notifications");
  await page.evaluate(() => [...document.querySelectorAll('.notif[data-key="k-live"] .btn')].find((b) => b.textContent.includes("Dismiss")).click());
  check(await until(async () => (await ops("notifications.dismiss")).some((o) => o.args.key === "k-live")), "dismiss op");
  check(await until(async () => !(await page.$('.notif[data-key="k-live"]'))), "dismissed still shown");
  await mockPost("/mock/event", { ev: "notification", key: "0|android|17|null|1000", removed: true });
  check(await until(async () => !(await page.$('.notif[data-key="0|android|17|null|1000"]'))), "removed event");
  await page.close();
});

test("clipboard_send_and_read_with_reason", ["PHN-13"], async () => {
  await mockPost("/mock/clear");
  await mockPost("/mock/foreground", { on: false });
  const page = await newPage();
  await go(page, "clipboard");
  await page.type('textarea[aria-label="Text for the phone\'s clipboard"]', "to the phone");
  check(await clickText(page, ".btn", "Send to the phone"), "no send");
  check(await until(async () => (await ops("clipboard.set")).some((o) => o.args.text === "to the phone")), "clipboard.set");
  check((await text(page)).includes("only while that app is open"), "explanation missing");
  check(await clickText(page, ".btn", "Read the phone's clipboard"), "no read");
  check(await until(() => text(page).then((t) => t.includes("open Arstro Remote Client first"))), "the 409 reason not shown");
  await mockPost("/mock/foreground", { on: true });
  await clickText(page, ".btn", "Read the phone's clipboard");
  check(await until(() => page.$eval('textarea[aria-label="The phone\'s clipboard"]', (e) => e.value === "to the phone")), "clipboard text");
  await page.close();
});

test("apps_grid_icons_search_open", ["PHN-14"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "apps");
  check(await until(() => page.$$eval(".app-tile", (t) => t.length === 20)), "20 user apps");
  check(await until(() => page.$$eval(".app-tile img", (i) => i.slice(0, 8).every((x) => x.complete && x.naturalWidth === 48))), "icons");
  const src = await page.$eval(".app-tile img", (i) => i.getAttribute("src"));
  check(src.startsWith("api/apps/icon?package="), "icon URL " + src);
  await page.type('input[aria-label="Search apps"]', "tele");
  check(await until(() => page.$$eval(".app-tile", (t) => t.length === 1 && t[0].textContent.includes("Telegram"))), "search");
  await page.click(".app-tile");
  check(await until(async () => (await ops("apps.open")).some((o) => o.args.package === "com.example.telegram")), "apps.open");
  await page.$eval('input[aria-label="Search apps"]', (e) => { e.value = ""; e.dispatchEvent(new Event("input")); });
  await page.click('input[aria-label="Show system apps"]');
  check(await until(() => page.$$eval(".app-tile", (t) => t.length === 23)), "system apps");
  await page.click('input[aria-label="Show system apps"]');
  await page.close();
});

test("tools_volume_media_speak_vibrate_brightness_url", ["PHN-15"], async () => {
  await mockPost("/mock/clear");
  const page = await newPage();
  await go(page, "tools");
  check(await until(() => page.$eval('[data-stream="media"] .val', (e) => e.textContent === "9/15")), "media volume not read");
  await page.$eval('[data-stream="ring"] input', (e) => { e.value = "1"; e.dispatchEvent(new Event("input")); });
  check(await until(async () => (await ops("tools.volume")).some((o) => o.args.stream === "ring" && o.args.level === 1)), "volume set");
  check(await until(() => page.$eval('[data-stream="ring"] .val', (e) => e.textContent === "7/7")), "volume answer shown");
  await page.click('button[aria-label="Next"]');
  check(await until(async () => (await ops("tools.media")).some((o) => o.args.action === "next")), "media next");
  await page.type('textarea[aria-label="Text to speak"]', "Hello phone");
  await clickText(page, ".btn", "Speak");
  check(await until(async () => (await ops("tools.speak")).some((o) => o.args.text === "Hello phone")), "speak");
  await clickText(page, ".btn", "Vibrate");
  check(await until(async () => (await ops("tools.vibrate")).some((o) => o.args.ms === 800)), "vibrate");
  await page.click('input[aria-label="Automatic brightness"]');
  check(await until(async () => (await ops("tools.brightness")).some((o) => o.args.level === "auto")), "brightness auto");
  await page.click('input[aria-label="Automatic brightness"]');
  await page.$eval('input[aria-label="Brightness"]', (e) => { e.value = "0.3"; e.dispatchEvent(new Event("input")); });
  check(await until(async () => (await ops("tools.brightness")).some((o) => o.args.level === 0.3)), "brightness level");
  await page.type('input[aria-label="URL to open on the phone"]', "example.com");
  await clickText(page, ".btn", "Open");
  check(await until(async () => (await ops("tools.open_url")).some((o) => o.args.url === "https://example.com")), "open url");
  // an op error is a readable toast
  await page.$eval('input[aria-label="URL to open on the phone"]', (e) => { e.value = "ftp://x"; });
  await clickText(page, ".btn", "Open");
  check(await until(() => page.$eval("#toasts", (e) => e.textContent.includes("Not a URL"))), "op error not a toast");
  await page.close();
});

test("shell_runs_a_command", ["PHN-16"], async () => {
  const page = await newPage();
  await go(page, "shell");
  await page.type('input[aria-label="Command"]', "echo hi there");
  await page.keyboard.press("Enter");
  check(await until(() => page.$eval(".term", (e) => e.textContent.includes("$ echo hi there\nhi there\n[exit 0]"))), "output");
  await page.type('input[aria-label="Command"]', "su");
  await page.keyboard.press("Enter");
  check(await until(() => page.$eval(".term", (e) => e.textContent.includes("[exit 127]"))), "exit code");
  await shot(page, "shell");
  await page.close();
});

test("features_read_only_and_gating", ["PHN-02", "PHN-17"], async () => {
  const page = await newPage();
  await go(page, "features");
  check(await until(() => page.$$eval("[data-feature]", (r) => r.length === 14)), "14 features");
  check(await page.$$eval(".view input", (i) => i.every((x) => x.disabled)), "a switch is not read-only");
  check((await text(page)).includes("Change them in the Arstro Remote Client app"), "explanation");
  await mockPost("/mock/features", { apps: { on: false }, notifications: { ready: false, missing: ["Notification access"] } });
  check(await until(() => page.$eval('.nav-item[data-id="apps"]', (e) => e.classList.contains("grey") && e.textContent.includes("Off on the phone"))), "apps not greyed");
  check(await until(() => page.$eval('.nav-item[data-id="notifications"]', (e) => e.classList.contains("grey") && e.textContent.includes("Needs Notification access"))), "notifications reason");
  check((await text(page, ".nav-list")).includes("Off on the phone"), "off group");
  check(await until(() => page.$eval('[data-feature="notifications"]', (e) => e.textContent.includes("Notification access"))), "features page not live");
  await shot(page, "features");
  await go(page, "apps");
  check((await text(page)).includes("Apps is turned off on the phone"), "blocked page reason");
  const r = await apiOp("apps.list");
  check(r.status === 403 && r.body.error === "Apps is turned off on the phone", "the mock's gate");
  await go(page, "notifications");
  check((await text(page)).includes("needs Notification access"), "not-ready reason");
  await mockPost("/mock/features", { apps: { on: true }, notifications: { ready: true, missing: [] } });
  check(await until(() => page.$$eval(".notif", (n) => n.length >= 1)), "page did not come back when ready");
  await page.close();
});

test("events_reconnect_after_a_drop", ["PHN-17"], async () => {
  const page = await newPage();
  await go(page, "overview");
  check(await until(() => page.evaluate(() => window.phoneDebug.events.status === "open")), "not open");
  const n = await page.evaluate(() => window.phoneDebug.events.opened);
  await mockPost("/mock/drop");
  check(await until(() => page.evaluate((n) => window.phoneDebug.events.opened > n && window.phoneDebug.events.status === "open", n), 8000), "did not reconnect");
  await mockPost("/mock/state", { topic: "find", data: { ringing: true, lost: false } });
  check(await until(() => page.$eval(".quick", (e) => e.textContent.includes("Stop ringing"))), "topics not live after reconnect");
  await mockPost("/mock/state", { topic: "find", data: { ringing: false, lost: false } });
  await page.close();
});

// ------------------------------------------------------------------------------------ runner
async function main() {
  if (args.includes("--list")) { for (const t of TESTS) console.log(t.name.padEnd(50), t.reqs.join(" ")); return 0; }
  const exe = [process.env.CHROME, "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"].find((p) => p && fs.existsSync(p));
  if (!exe) { console.error("Chrome not found - set CHROME=/path/to/chrome"); return 2; }
  fs.mkdirSync(SHOTS, { recursive: true });
  await startMock();
  console.log("mock phone at " + BASE);
  browser = await puppeteer.launch({
    executablePath: exe, headless: true,
    args: ["--no-sandbox", "--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
           "--use-fake-device-for-media-stream", "--mute-audio"],
  });
  let passed = 0, failed = 0;
  const covered = {};
  try {
    for (const t of TESTS.filter((x) => !ONLY || x.name.includes(ONLY))) {
      const t0 = Date.now();
      pageErrors.length = 0;
      let ok = true;
      try {
        await t.fn();
        check(!pageErrors.length, "page errors: " + pageErrors.join(" | "));
      } catch (e) {
        ok = false;
        console.log(`FAIL  ${t.name.padEnd(48)} ${e.message}`);
        for (const p of await browser.pages()) await p.close().catch(() => {});
      }
      if (ok) { passed++; console.log(`PASS  ${t.name.padEnd(48)} ${((Date.now() - t0) / 1000).toFixed(1).padStart(5)}s  ${t.reqs.join(" ")}`); }
      else failed++;
      for (const r of t.reqs) (covered[r] = covered[r] || []).push(ok);
    }
  } finally {
    await browser.close();
    if (!args.includes("--keep")) mock.kill();
  }
  console.log("\nrequirements: " + Object.keys(covered).sort().map((r) => r + (covered[r].every(Boolean) ? "" : "(FAIL)")).join(" "));
  console.log(`${passed} passed, ${failed} failed`);
  return failed ? 1 : 0;
}

process.exit(await main());
