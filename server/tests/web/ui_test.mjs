#!/usr/bin/env node
// Browser tests of the web controller against a running server (requirement-tagged).
//
//   cd server/tests/web && npm install
//   ARSTRO_URL=http://<pi>:8080/ ARSTRO_TOKEN=<token> node ui_test.mjs [-k name] [--list] [--shots DIR]
//
// Optional: CHROME=/path/to/chrome, ARSTRO_URL_SECURE=http://127.0.0.1:<port>/ (an SSH tunnel to the
// same server: localhost is a secure context, so the WebCodecs preview path is tested too).
// The recorder is switched to its test pattern while testing (REC-08) and back afterwards;
// the test recording it makes is deleted again.

import fs from "node:fs";
import path from "node:path";
import puppeteer from "puppeteer-core";

const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const BASE = (process.env.ARSTRO_URL || "").replace(/\/?$/, "/");
const TOKEN = process.env.ARSTRO_TOKEN || "";
const SECURE = process.env.ARSTRO_URL_SECURE ? process.env.ARSTRO_URL_SECURE.replace(/\/?$/, "/") : null;
const SHOTS = opt("--shots");
const ONLY = opt("-k");

const TESTS = [];
const test = (name, reqs, fn) => TESTS.push({ name, reqs, fn });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function check(cond, msg) { if (!cond) throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg)); }
async function until(fn, timeout = 10000, every = 150) {
  const end = Date.now() + timeout;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(every);
  }
}

// ------------------------------------------------------------ server REST (the "other controller")
async function op(name, body = {}, token = TOKEN) {
  const r = await fetch(BASE + "api/op/" + name, {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (!d.ok) throw new Error(`${name}: ${d.error}`);
  return d.data;
}

function findChrome() {
  const c = [process.env.CHROME, "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium",
             "/usr/bin/chromium-browser", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"];
  return c.find((p) => p && fs.existsSync(p));
}

// ------------------------------------------------------------------------------------ browser
let browser;
const pageErrors = [];

async function newPage(url = BASE, { width = 1280, height = 800, mobile = false, login = true } = {}) {
  const page = await browser.newPage();
  await page.setViewport({ width, height, isMobile: mobile, hasTouch: mobile });
  page.on("pageerror", (e) => pageErrors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/status of 40[14]/.test(m.text())) pageErrors.push(m.text()); });
  await page.goto(url, { waitUntil: "domcontentloaded" });
  if (login) {
    await page.waitForSelector(".nav-item, .login-card", { timeout: 10000 });
    if (await page.$(".login-card")) {
      await page.type("input[type=password]", TOKEN);
      await page.click("button[type=submit]");
    }
    await page.waitForSelector(".view", { timeout: 15000 });
  }
  return page;
}

async function go(page, view) {
  await page.evaluate((v) => { location.hash = "#/" + v; }, view);
  await page.waitForSelector(`.view[data-view="${view}"]`, { timeout: 8000 });
  await sleep(400);
}

async function shot(page, name) {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name + ".png") });
}

const clickText = (page, sel, text) => page.evaluate((sel, text) => {
  const el = [...document.querySelectorAll(sel)].find((e) => e.textContent.trim().includes(text) && !e.disabled);
  if (el) el.click();
  return !!el;
}, sel, text);

// --------------------------------------------------------------------------------------- tests
test("login_wrong_then_right_password_and_cookie", ["CON-03", "SEC-03"], async () => {
  const ping = await (await fetch(BASE + "api/ping")).json();
  if (ping.auth === "open") { console.log("      (skipped: server is in open mode)"); return; }
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".login-card input[type=password]");
  await page.type("input[type=password]", "not-the-token");
  await page.click("button[type=submit]");
  check(await until(() => page.$eval(".login-card .err", (e) => e.textContent).then((t) => t.includes("not right")), 5000),
        "wrong token not refused");
  await page.$eval("input[type=password]", (e) => { e.value = ""; });
  await page.type("input[type=password]", TOKEN);
  await page.click("button[type=submit]");
  await page.waitForSelector(".view", { timeout: 15000 });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".view", { timeout: 15000 });           // cookie remembered
  check(!(await page.$(".login-card")), "login asked again after reload");
  const cookies = await page.cookies();
  const c = cookies.find((x) => x.name === "arstro_token");
  check(c && c.httpOnly && c.sameSite === "Strict", "cookie must be HttpOnly + SameSite=Strict");
  await ctx.close();
});

const VIEWS = ["recorder", "gallery", "monitor", "wifi", "terminal", "remote", "system"];
test("every_view_fits_small_to_large_screens", ["UX-03", "UX-01"], async () => {
  for (const [w, h, mobile] of [[360, 640, true], [412, 915, true], [1280, 800, false], [1920, 1080, false]]) {
    const page = await newPage(BASE, { width: w, height: h, mobile });
    for (const v of VIEWS) {
      await go(page, v);
      await sleep(v === "monitor" || v === "wifi" ? 1200 : 300);
      const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
      check(sw <= iw, `${v} overflows horizontally at ${w}x${h}: ${sw} > ${iw}`);
      const nav = await page.$eval(mobile ? ".tabbar" : ".side", (e) => getComputedStyle(e).display);
      check(nav !== "none", `navigation hidden at ${w}x${h}`);
      await shot(page, `${w}x${h}-${v}`);
    }
    await page.close();
  }
});

test("preview_shows_live_test_pattern", ["REC-02", "REC-08", "REC-06"], async () => {
  const page = await newPage();
  await go(page, "recorder");
  const playing = await until(() => page.$(".preview.playing"), 20000);
  check(playing, "no live picture within 20 s");
  const moving = await until(() => page.evaluate(() => {
    const v = document.querySelector(".preview video.on");
    const c = document.querySelector(".preview canvas.on");
    if (c) return c.width > 0;
    return v && v.currentTime > 0.3 && v.videoWidth > 0;
  }), 8000);
  check(moving, "preview is not advancing");
  const st = await op("recorder.status");
  check(st.preview.viewers >= 1 && st.preview.on, "server does not count the viewer");
  const mode = await page.evaluate(() => (document.querySelector(".preview canvas.on") ? "webcodecs" : "mse"));
  console.log(`      preview via ${mode}`);
  await shot(page, "preview-live");
  await page.close();
  // capture stops ~5 s after the last viewer (REC-06)
  const off = await until(async () => !(await op("recorder.status")).preview.on, 12000, 500);
  check(off, "preview encoder still running 12 s after the last viewer left");
});

test("preview_webcodecs_in_secure_context", ["REC-02"], async () => {
  if (!SECURE) { console.log("      (skipped: set ARSTRO_URL_SECURE to a localhost tunnel)"); return; }
  const page = await newPage(SECURE);
  await go(page, "recorder");
  check(await until(() => page.$(".preview.playing canvas.on"), 20000), "WebCodecs preview did not start");
  await page.close();
});

test("settings_change_in_web_reaches_server_and_back", ["REC-04", "ARC-03"], async () => {
  const before = await op("recorder.settings.get");
  const page = await newPage();
  await go(page, "recorder");
  await page.click(".mode-card");
  await page.waitForSelector(".sheet.show");
  const target = before.preview.quality === "low" ? ["1080p", "high"] : ["360p", "low"];
  check(await clickText(page, ".sheet .seg button", target[0]), "quality button not found");
  check(await until(async () => (await op("recorder.settings.get")).preview.quality === target[1], 3000),
        "server did not get the web's setting");
  await op("recorder.settings.set", { settings: { preview: { quality: "medium" } } });   // "from the CLI"
  const t0 = Date.now();
  check(await until(() => page.evaluate(() =>
    [...document.querySelectorAll(".sheet .seg button.active")].some((b) => b.textContent === "720p")), 3000),
    "open settings sheet did not follow the other controller");
  console.log(`      cli -> web in ${Date.now() - t0} ms`);
  await shot(page, "settings");
  await op("recorder.settings.set", { settings: { preview: { quality: before.preview.quality } } });
  await page.close();
});

let recorded = null;
test("record_button_starts_and_stops_for_everyone", ["REC-03", "ARC-03", "UX-02"], async () => {
  const page = await newPage();
  await go(page, "recorder");
  await page.waitForSelector(".rec-btn:not([disabled])", { timeout: 15000 });
  await page.click(".rec-btn");
  const st = await until(async () => { const s = await op("recorder.status"); return s.recording.active && s; }, 10000);
  check(st, "recording did not start");
  recorded = st.recording.file;
  check(await until(() => page.$(".rec-btn.on"), 3000), "record button did not turn into stop");
  await sleep(3000);
  const t = await page.$eval(".rec-time", (e) => e.textContent);
  check(t !== "0:00", "timer is not running: " + t);
  await shot(page, "recording");
  await op("recorder.stop");                                           // stopped by another controller
  check(await until(() => page.$(".rec-btn:not(.on)"), 8000), "web did not see the stop from the other controller");
  await page.close();
});

test("gallery_take_convert_play_delete", ["GAL-01", "GAL-02", "GAL-03", "GAL-04", "GAL-05", "GAL-06", "GAL-07"], async () => {
  check(recorded, "needs the recording of the previous test");
  const takeId = recorded.replace(/\.[^.]+$/, "").replace(/_(H265|H264.*|FFV1)$/, "");
  const page = await newPage();
  await go(page, "gallery");
  const card = await until(() => page.evaluateHandle((id) =>
    [...document.querySelectorAll(".take")].find((c) => c.querySelector("img")?.src.includes(id)) || null, takeId)
    .then((h) => h.asElement()), 15000);
  check(card, "new take not listed");
  await card.click();
  await page.waitForSelector(".sheet.show .variant");
  // convert to the share format (H.264 720p)
  await page.click(".sheet.show .variant button[title=Convert]");
  await until(() => page.$$eval(".sheet.show", (s) => s.length >= 2), 5000);
  check(await clickText(page, ".sheet.show .target", "H.264"), "H.264 target missing");
  await page.select(".sheet.show:last-of-type select", "720").catch(() => {});
  check(await clickText(page, ".sheet.show .sheet-foot button", "Convert"), "convert button missing");
  const job = await until(async () => (await op("jobs.list")).jobs.find((j) => j.source.startsWith(takeId) && j.codec === "h264-vpu"), 8000);
  check(job, "conversion job not created");
  check(await until(() => page.$(".card .job"), 8000), "job not shown in the web");
  const done = await until(async () => (await op("jobs.list")).jobs.find((j) => j.id === job.id && j.state !== "queued" && j.state !== "running"), 120000, 1000);
  check(done, "conversion did not finish within 2 min");
  check(done.state === "done", `conversion ${done.state}: ${done.error}`);
  // the take sheet shows the new variant and plays it
  const h264 = await until(() => page.evaluate(() =>
    [...document.querySelectorAll(".sheet.show .variant .name")].some((n) => n.textContent.includes("H.264"))), 15000);
  check(h264, "new H.264 file not shown in the take");
  await page.evaluate(() => {
    const row = [...document.querySelectorAll(".sheet.show .variant")].find((r) => r.textContent.includes("H.264"));
    row.querySelector("button[title=Play]")?.click();
  });
  const ready = await until(() => page.evaluate(() => {
    const v = document.querySelector(".sheet.show .player video");
    return v && v.readyState >= 1 && v.duration > 0;
  }), 15000);
  check(ready, "H.264 copy does not play in the browser");
  await shot(page, "take");
  // delete just the H.264 variant, then the whole take
  await page.evaluate(() => {
    const row = [...document.querySelectorAll(".sheet.show .variant")].find((r) => r.textContent.includes("H.264"));
    row.querySelector("button[aria-label=Delete]").click();
  });
  await page.waitForSelector(".sheet.show .btn.danger.solid");
  await page.click(".sheet.show .btn.danger.solid");
  check(await until(async () => {
    const t = await op("gallery.get", { take: takeId }).catch(() => null);
    return t && !t.items.some((i) => i.kind === "H.264");
  }, 8000), "H.264 variant not deleted");
  await sleep(500);
  check(await clickText(page, ".sheet.show .sheet-foot button", "Delete take"), "delete take button missing");
  await page.waitForSelector(".sheet.show .btn.danger.solid");
  await page.click(".sheet.show .btn.danger.solid");
  check(await until(async () => !(await op("gallery.list")).takes.some((t) => t.id === takeId), 8000), "take not deleted");
  check(await until(() => page.evaluate((id) => ![...document.querySelectorAll(".take img")].some((i) => i.src.includes(id)), takeId), 8000),
        "deleted take still shown");
  recorded = null;
  await page.close();
});

test("verified_ffv1_replaces_the_raw", ["GAL-08", "REC-04", "ARC-03"], async () => {
  const before = await op("recorder.settings.get");
  const page = await newPage();
  try {
    await op("recorder.settings.set", { settings: { mode: "raw", raw: { ffv1: true, ffv1_engine: "cpu", when: "after", hq: false, ffv1_replace_raw: false } } });
    await go(page, "recorder");
    await page.click(".mode-card");
    await page.waitForSelector(".sheet.show");
    // the switch lives in the sheet; turn it on from the web and see it on the server
    const on = await page.evaluate(() => {
      const row = [...document.querySelectorAll(".sheet.show .set-row")].find((r) => r.textContent.includes("Delete RAW after a verified copy"));
      const input = row && row.querySelector("input[type=checkbox]");
      if (input) input.click();
      return !!input;
    });
    check(on, "no 'delete RAW after a verified copy' switch in the settings sheet");
    check(await until(async () => (await op("recorder.settings.get")).raw.ffv1_replace_raw === true, 3000), "setting not saved");
    await page.keyboard.press("Escape");
    await op("recorder.start");
    await sleep(2500);
    const stop = await op("recorder.stop");
    const take = stop.file.replace(/\.arh$/, "");
    const job = await until(async () => (await op("jobs.list")).jobs.find((j) => j.source === take + ".arh" && j.state === "done" && j.verify), 180000, 1000);
    check(job && job.verify.ok, "copy not verified: " + JSON.stringify(job && job.verify));
    check(await until(async () => (await op("jobs.list")).jobs.find((j) => j.id === job.id && j.verify.raw_deleted), 10000), "RAW not deleted");
    await go(page, "gallery");
    check(await until(() => page.$eval(".view", (e) => e.textContent.includes("Verified · RAW deleted")), 8000), "jobs list does not show the verdict");
    const card = await until(() => page.evaluateHandle((id) =>
      [...document.querySelectorAll(".take")].find((c) => c.querySelector("img")?.src.includes(id)) || null, take).then((h) => h.asElement()), 10000);
    check(card, "take not listed");
    await card.click();
    await page.waitForSelector(".sheet.show .variant");
    const txt = await page.$eval(".sheet.show", (e) => e.textContent);
    check(txt.includes("Verified lossless") && !txt.includes(".arh"), "take should hold only the verified FFV1: " + txt.slice(0, 200));
    await shot(page, "verified");
    await op("gallery.delete_take", { take });
  } finally {
    await op("recorder.settings.set", { settings: { mode: before.mode, raw: before.raw } });
    await page.close();
  }
});

test("terminal_shared_between_browsers", ["TERM-01", "TERM-02", "TERM-04", "ARC-03"], async () => {
  const a = await newPage();
  await go(a, "terminal");
  await a.waitForSelector(".term-tabs .btn");
  await clickText(a, ".term-tabs .btn, .term-box .btn", "shell");
  await a.waitForSelector(".term-box .xterm", { timeout: 10000 });
  const list = await until(async () => (await op("term.list")).terminals, 5000);
  check(list && list.length, "no shell on the server");
  const id = Math.max(...list.map((t) => t.term));
  await a.click(".term-box .xterm");
  await a.keyboard.type("echo WEB_$((6*7))\n");
  check(await until(() => a.$eval(".term-box .xterm-rows", (e) => e.textContent.includes("WEB_42")), 8000), "no shell output");
  const b = await newPage();
  await go(b, "terminal");
  await until(() => b.evaluate((id) => !!document.querySelector(".term-tab"), id), 5000);
  await clickText(b, ".term-tab", `Shell ${id}`);
  check(await until(() => b.$eval(".term-box .xterm-rows", (e) => e.textContent.includes("WEB_42")).catch(() => false), 8000),
        "second browser did not get the replay");
  await b.click(".term-box .xterm");
  await b.keyboard.type("echo FROM_B_$((2+3))\n");
  await a.bringToFront();                    // background tabs do not repaint xterm
  check(await until(() => a.$eval(".term-box .xterm-rows", (e) => e.textContent.includes("FROM_B_5")), 8000), "not mirrored to the first browser");
  const t = (await op("term.list")).terminals.find((x) => x.term === id);
  check(t && t.viewers >= 2, "server does not show 2 viewers");
  await shot(a, "terminal");
  await op("term.close", { term: id }).catch(() => {});
  await a.close();
  await b.close();
});

test("touchpad_moves_the_pointer", ["INP-04", "INP-01"], async () => {
  const p0 = await op("in.pointer").catch(() => null);
  if (!p0) { console.log("      (skipped: no input backend)"); return; }
  const page = await newPage();
  await go(page, "remote");
  const box = await (await page.$(".pad")).boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  for (let i = 0; i < 10; i++) { await page.mouse.move(box.x + box.width / 2 + i * 8, box.y + box.height / 2 + i * 4); await sleep(20); }
  await page.mouse.up();
  await sleep(400);
  const p1 = await op("in.pointer");
  check(p1.x !== p0.x || p1.y !== p0.y, `pointer did not move (${JSON.stringify(p0)} -> ${JSON.stringify(p1)})`);
  await page.mouse.move(box.x + 50, box.y + 50);
  await page.goto("about:blank");
  await op("in.move_to", { x: p0.x, y: p0.y }).catch(() => {});
  await page.close();
});

test("wifi_monitor_system_show_server_state", ["WIFI-01", "STAT-01", "STAT-02", "CON-05", "ADM-01", "ADM-03"], async () => {
  const wifi = await op("wifi.status");
  const page = await newPage();
  await go(page, "wifi");
  if (wifi.connected) {
    check(await until(() => page.$eval(".view", (e, s) => e.textContent.includes(s), wifi.ssid), 5000), "SSID not shown");
  }
  await go(page, "monitor");
  check(await until(() => page.$eval(".view", (e) => e.textContent.includes("CPU") && e.textContent.includes("Memory")), 6000), "stats not shown");
  const t1 = await page.$eval(".view .big", (e) => e.textContent);
  check(await until(async () => (await op("admin.status")).sessions.some((s) => s.stats_subscribed), 4000), "stats not subscribed");
  await go(page, "system");
  check(await until(() => page.$eval(".view", (e) => e.textContent.includes("this page")), 5000), "own controller not listed");
  const info = await op("web.info");
  check(await until(() => page.$eval(".view", (e, u) => e.textContent.includes(u), info.urls[0] || ""), 5000), "web URL not shown");
  void t1;
  await page.close();
});

// ------------------------------------------------------------------------------------ runner
async function main() {
  if (args.includes("--list")) { for (const t of TESTS) console.log(t.name.padEnd(46), t.reqs.join(" ")); return 0; }
  if (!BASE.startsWith("http")) { console.error("set ARSTRO_URL (and ARSTRO_TOKEN = the access password)"); return 2; }
  const exe = findChrome();
  if (!exe) { console.error("Chrome not found - set CHROME=/path/to/chrome"); return 2; }
  if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
  browser = await puppeteer.launch({ executablePath: exe, headless: true,
                                     args: ["--no-sandbox", "--autoplay-policy=no-user-gesture-required", "--mute-audio"] });
  const before = await op("recorder.status");
  const savedSettings = await op("recorder.settings.get");
  if (!before.simulate) await op("recorder.source", { simulate: "1280x720@30" });
  // a small recording format for the test
  await op("recorder.settings.set", { settings: { mode: "h265", h265: { bitrate: 8 } } });
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
        console.log(`FAIL  ${t.name.padEnd(44)} ${e.message}`);
      }
      if (ok) { passed++; console.log(`PASS  ${t.name.padEnd(44)} ${((Date.now() - t0) / 1000).toFixed(1).padStart(5)}s  ${t.reqs.join(" ")}`); }
      else failed++;
      for (const r of t.reqs) (covered[r] = covered[r] || []).push(ok);
    }
  } finally {
    if (recorded) {
      const takeId = recorded.replace(/\.[^.]+$/, "");
      await op("gallery.delete_take", { take: takeId }).catch(() => {});
    }
    await op("recorder.settings.set", { settings: { mode: savedSettings.mode, h265: savedSettings.h265 } }).catch(() => {});
    if (!before.simulate) await op("recorder.source", { simulate: null }).catch((e) => console.log("restore source:", e.message));
    await browser.close();
  }
  console.log("\nrequirements: " + Object.keys(covered).sort().map((r) => r + (covered[r].every(Boolean) ? "" : "(FAIL)")).join(" "));
  console.log(`${passed} passed, ${failed} failed`);
  return failed ? 1 : 0;
}

process.exit(await main());
