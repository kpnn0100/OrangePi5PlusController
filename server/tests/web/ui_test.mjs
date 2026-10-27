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
  const c = cookies.find((x) => x.name.startsWith("arstro_token"));
  check(c && c.httpOnly && c.sameSite === "Strict", "cookie must be HttpOnly + SameSite=Strict");
  await ctx.close();
});

const VIEWS = ["recorder", "gallery", "screen", "monitor", "wifi", "network", "bluetooth", "terminal", "remote",
               "pins", "i2c", "spi", "uart", "pwm", "adc", "files", "system", "logs"];

test("pages_are_grouped_by_the_servers_modules", ["MOD-04", "CAM-03", "ADM-07"], async () => {
  const page = await newPage();
  const hello = await page.evaluate(async () => (await (await fetch("/api/ping")).json()));
  const groups = await page.$$eval(".side .nav-item span", (els) => els.map((e) => e.textContent));
  check(groups.includes("System") && groups.length <= 9, "groups: " + groups.join(", "));
  await go(page, "gallery");                                            // an old link still works
  const subs = await page.$$eval(".subnav button", (els) => els.map((e) => e.textContent));
  check(subs.join("/") === "Live/Gallery", "camera sub-tabs: " + subs.join("/"));
  check(await page.$eval(".view", (e) => e.dataset.group) === "camera", "gallery is not in the Camera group");
  if (hello.slot) {
    const host = await page.$eval(".side .brand-host", (e) => e.textContent);
    check(host.endsWith(hello.slot.toUpperCase()), "slot not shown: " + host);
  }
  await page.close();
});
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
  if (!playing) {
    const why = await page.evaluate(() => {
      const p = window.arstroPreview, v = document.querySelector(".preview video");
      return JSON.stringify({ mode: p && p.mode, events: p && p.events, rs: v && v.readyState, ct: v && v.currentTime,
                              paused: v && v.paused, err: v && v.error && v.error.message,
                              buf: v && v.buffered.length ? [v.buffered.start(0), v.buffered.end(v.buffered.length - 1)] : null });
    });
    check(false, "no live picture within 20 s: " + why);
  }
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

test("gallery_take_convert_play_delete", ["GAL-01", "GAL-02", "GAL-03", "GAL-04", "GAL-05", "GAL-06", "GAL-07", "GAL-12"], async () => {
  check(recorded, "needs the recording of the previous test");
  const takeId = recorded.replace(/\.[^.]+$/, "").replace(/_(H265|H264.*|FFV1)$/, "");
  const page = await newPage();
  await go(page, "gallery");
  const card = await until(() => page.evaluateHandle((id) =>
    [...document.querySelectorAll(".take")].find((c) => c.querySelector("img")?.src.includes(id)) || null, takeId)
    .then((h) => h.asElement()), 15000);
  check(card, "new take not listed");
  // GAL-01: under a month and a day heading; GAL-12: select it, download one ZIP
  check(await page.evaluate((c) => !!c.closest(".day")?.querySelector(".day-head h3") &&
                                   !!c.closest(".month")?.querySelector(".month-head h2"), card), "take is not under a month / day heading");
  check(await clickText(page, ".view-head button", "Select"), "Select button missing");
  await card.click();                                                   // in select mode a click selects
  check(await until(() => page.$(".selbar.on"), 3000), "selection bar not shown");
  check(await card.evaluate((c) => c.classList.contains("selected")), "card not marked selected");
  const info = await page.$eval(".selbar", (b) => b.textContent);
  check(/^1 take · \d+ files?/.test(info), "selection sum: " + info);
  await page.evaluate(() => { window.__dl = []; HTMLAnchorElement.prototype.click = function () { window.__dl.push([this.href, this.download]); }; });
  check(await clickText(page, ".selbar button", "Download ZIP"), "Download ZIP button missing");
  const dl = await until(() => page.evaluate(() => window.__dl[0] || null), 8000);
  check(dl && /\/api\/bundle\/\w+\.zip$/.test(dl[0]) && dl[1].endsWith(".zip"), "no ZIP download: " + JSON.stringify(dl));
  const head = await page.evaluate(async (u) => {
    const r = await fetch(u, { method: "HEAD" });
    return [r.status, r.headers.get("content-type"), Number(r.headers.get("content-length"))];
  }, dl[0]);
  check(head[0] === 200 && head[1] === "application/zip" && head[2] > 0, "bundle: " + head.join(" "));
  await shot(page, "gallery-select");
  check(await clickText(page, ".view-head button", "Done"), "Done button missing");
  check(await until(() => page.$(".selbar:not(.on)"), 3000), "selection bar still shown");
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

test("extra_copies_by_codec", ["REC-04", "ARC-03"], async () => {
  // one collapsible sub-section per kind of copy - H.264, H.265, FFV1 - switch in the header
  const before = await op("recorder.settings.get");
  const page = await newPage();
  try {
    await op("recorder.settings.set", { settings: { mode: "raw", raw: { h264: false, hq: false, ffv1: false } } });
    await go(page, "recorder");
    await page.click(".mode-card");
    await page.waitForSelector(".sheet.show .copy-head");
    const names = await page.evaluate(() => [...document.querySelectorAll(".sheet.show .copy-head b")].map((b) => b.textContent));
    check(JSON.stringify(names) === JSON.stringify(["H.264", "H.265", "FFV1"]), "copy sections: " + names);
    const isOpen = () => page.evaluate(() => document.querySelector(".sheet.show .copy-sec").classList.contains("open"));
    const wasOpen = await isOpen();
    await page.click('.sheet.show .copy-head[data-copy="h264"] .txt');
    check(await until(async () => (await isOpen()) !== wasOpen, 2000), "the H.264 header did not open / close its options");
    await page.click('.sheet.show .copy-head[data-copy="h264"] .txt');
    check(await until(async () => (await isOpen()) === wasOpen, 2000), "the H.264 header did not toggle back");
    // the switch turns the copy on (on the server) and opens its options; the header then says them
    await page.evaluate(() => document.querySelector('.sheet.show .copy-head[data-copy="h264"] input[type=checkbox]').click());
    check(await until(async () => (await op("recorder.settings.get")).raw.h264 === true, 3000), "H.264 copy not switched on");
    check(await until(isOpen, 3000), "switching the copy on did not open its options");
    const sub = await until(() => page.evaluate(() => {
      const t = document.querySelector('.sheet.show .copy-head[data-copy="h264"] small');
      return t && t.textContent.includes("·") ? t.textContent : null;
    }), 3000);
    check(sub, "the H.264 header does not show its options");
    await page.keyboard.press("Escape");
  } finally {
    await op("recorder.settings.set", { settings: { mode: before.mode, raw: { h264: before.raw.h264 || false, hq: before.raw.hq, ffv1: before.raw.ffv1 } } });
    await page.close();
  }
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

// ------------------------------------------------------------------- film work (REC-09..16, GAL-13)
const MON_OFF = { guides: "off", safe: false, center: false, zebra: false, false_color: false, scope: "off", meters: true };

/** Wait for the live picture; returns false (and logs why) when there is none. */
async function livePicture(page) {
  await go(page, "recorder");
  return !!(await until(() => page.$(".preview.playing"), 20000));
}

test("live_meters_overlays_scope_and_tally", ["REC-10", "REC-13", "REC-12"], async () => {
  const before = await op("recorder.settings.get");
  const st = await op("recorder.status");
  const page = await newPage();
  try {
    if (before.mode === "h265" && !(st.caps && st.caps.vpu_h265)) await op("recorder.settings.set", { settings: { mode: "raw" } });
    await op("recorder.settings.set", { settings: { monitor: { guides: "off", safe: false, center: false, zebra: true, zebra_level: 50,
                                                               false_color: true, scope: "waveform", meters: true } } });
    check(await livePicture(page), "no live picture within 20 s");
    // the player asked for meters and exposure data (hello); the levels fill the meters
    const want = await page.evaluate(() => window.arstroPreview.want);
    check(want.includes("levels") && want.includes("analysis"), "hello want: " + JSON.stringify(want));
    check(await until(() => page.$$eval(".meter-rows .meter:not(.scale)", (r) => r.length), 8000), "no audio meter rows");
    // overlays (guides, safe areas, cross; zebras and false colour from the exposure map) and the waveform
    const covered = () => page.evaluate(() => {
      const c = document.querySelector(".preview .mon-ov");
      const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      let n = 0;
      for (let i = 3; i < d.length; i += 16) if (d[i]) n++;
      return n / (d.length / 16);
    });
    if (st.caps && st.caps.analysis) {
      check(await until(async () => (await covered()) > 0.2, 8000), "zebras / false colour do not cover the picture");
      const wave = await until(() => page.evaluate(() => {
        const c = document.querySelector(".scope");
        const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
        let n = 0;
        for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 40 && d[i + 1] > d[i] + 40) n++;
        return n;
      }), 8000);
      check(wave, "the waveform shows nothing");
    } else console.log("      (no exposure data on this board: overlays of guides only)");
    // a 2.39 guide shades about a quarter of a 16:9 picture; safe areas and the cross are lines
    await op("recorder.settings.set", { settings: { monitor: { zebra: false, false_color: false, guides: "2.39", safe: true, center: true } } });
    const guidesOnly = await until(async () => { const c = await covered(); return c > 0.2 && c < 0.4 ? c : null; }, 4000);
    check(guidesOnly, "the 2.39 guide is not drawn alone: " + (await covered()).toFixed(3));
    // storage you can trust (REC-12): the time left at the mode's rate
    check(await page.$eval(".rec-stats", (e) => e.textContent.includes("Time left")), "no time left shown");
    await shot(page, "live-monitoring");
    // meters off: hidden, and the player stops asking for levels
    await op("recorder.settings.set", { settings: { monitor: { meters: false, scope: "off" } } });
    check(await until(() => page.$(".mon-strip.hidden"), 3000), "meters / scope still shown");
    check(!(await page.evaluate(() => window.arstroPreview.want)).includes("levels"), "still asking for levels");
    // REC-13 tally: a red frame while recording
    await op("recorder.start");
    check(await until(() => page.$(".preview.tally"), 6000), "no tally while recording");
    await shot(page, "live-tally");
    await op("recorder.stop");
    check(await until(() => page.$(".preview:not(.tally)"), 10000), "tally still on after the stop");
    const last = (await op("recorder.status")).last;
    if (last && last.file) await op("gallery.delete_take", { take: last.file.replace(/\.[^.]+$/, "").replace(/_(H265|H264.*|FFV1)$/, "") }).catch(() => {});
  } finally {
    if ((await op("recorder.status")).recording.active) await op("recorder.stop").catch(() => {});
    await op("recorder.settings.set", { settings: { mode: before.mode, monitor: { ...MON_OFF, ...before.monitor } } });
    await page.close();
  }
});

test("focus_zoom_click_moves_and_esc", ["REC-14"], async () => {
  const page = await newPage();
  try {
    check(await livePicture(page), "no live picture within 20 s");
    await page.evaluate(() => {
      window.__sent = [];
      const ws = window.arstroPreview.ws, send = ws.send.bind(ws);
      ws.send = (d) => { window.__sent.push(JSON.parse(d)); send(d); };
    });
    const sig = (await op("recorder.status")).signal;
    const whole = await page.evaluate(() => window.arstroPreview.config && window.arstroPreview.config.width);
    // click the picture at 75 % / 30 % of the whole picture (the box is 16:9 like the test pattern)
    const box = await page.$eval(".preview", (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; });
    await page.mouse.click(box[0] + box[2] * 0.75, box[1] + box[3] * 0.3);
    const sent = await until(() => page.evaluate(() => window.__sent.find((m) => m.type === "zoom") || null), 3000);
    check(sent && sent.on === true && Math.abs(sent.x - 0.75) < 0.03 && Math.abs(sent.y - 0.3) < 0.03, "zoom message: " + JSON.stringify(sent));
    check(await until(() => page.$(".preview.zoomed"), 8000), "the zoom reply did not arrive");
    const badge = await page.$eval(".zoom-badge", (e) => e.textContent);
    check(/^Zoom \d+× · /.test(badge), "zoom badge: " + badge);
    // the video now is the crop's (a new config, smaller than the whole picture), at full resolution
    const cfg = await until(() => page.evaluate(() => { const c = window.arstroPreview.config; return c && c.zoom ? c : null; }), 5000);
    check(cfg && cfg.width < sig.width, "video is not the crop: " + JSON.stringify(cfg));
    check(await until(() => page.$(".preview.playing.zoomed"), 8000), "the zoomed picture does not play");
    check(await until(async () => (await op("recorder.status")).monitor.zooms === 1, 3000), "server does not count the zoom");
    check(!(await page.$eval(".zoom-loc", (e) => e.classList.contains("hidden"))), "no locator while zoomed");
    await shot(page, "focus-zoom");
    // clicking the zoomed picture moves the zoom: a point inside the crop -> whole-picture coordinates
    const z = await page.evaluate(() => window.__sent.length);
    await page.mouse.click(box[0] + box[2] * 0.1, box[1] + box[3] * 0.5);
    const moved = await until(() => page.evaluate((n) => window.__sent.slice(n).find((m) => m.type === "zoom") || null, z), 3000);
    check(moved && moved.on && moved.x < sent.x && moved.x > sent.x - 0.4, "move: " + JSON.stringify(moved));
    // Esc: the whole picture again
    await page.keyboard.press("Escape");
    check(await until(() => page.evaluate(() => window.__sent.some((m) => m.type === "zoom" && m.on === false)), 3000), "Esc sent no zoom off");
    check(await until(() => page.$(".preview.playing:not(.zoomed)"), 8000), "the whole picture did not come back");
    // the whole picture at the preview's own size again (whatever quality the board is set to)
    check(await until(() => page.evaluate((w) => window.arstroPreview.config && window.arstroPreview.config.width === w && !window.arstroPreview.config.zoom,
                                         whole), 5000), "video is still the crop");
    check(await until(async () => (await op("recorder.status")).monitor.zooms === 0, 3000), "server still zooms");
  } finally {
    await page.close();
  }
});

test("monitoring_slate_gpio_settings_round_trip", ["REC-13", "REC-14", "REC-15", "REC-16", "REC-12", "REC-09", "ARC-03"], async () => {
  const before = await op("recorder.settings.get");
  const page = await newPage();
  try {
    await go(page, "recorder");
    await page.click(".mode-card");
    await page.waitForSelector(".sheet.show .mon-set");
    const titles = await page.$$eval(".sheet.show .set-group-title", (e) => e.map((x) => x.textContent));
    for (const t of ["Audio", "Slate", "Monitoring", "Storage", "Record button & tally"]) check(titles.includes(t), "no settings group " + t);
    const get = async () => op("recorder.settings.get");
    // web -> server
    await page.select('.sheet.show select[aria-label="Frame guides"]', "1.85");
    check(await until(async () => (await get()).monitor.guides === "1.85", 3000), "guides not saved");
    const target = before.monitor.zoom === 4 ? ["6×", 6] : ["4×", 4];
    check(await clickText(page, ".sheet.show .mon-set .seg button", target[0]), "zoom ratio button missing");
    check(await until(async () => (await get()).monitor.zoom === target[1], 3000), "zoom ratio not saved");
    await page.evaluate(() => document.querySelector('.sheet.show input[aria-label="Safe areas"]').click());
    check(await until(async () => (await get()).monitor.safe === !before.monitor.safe, 3000), "safe areas not saved");
    const scene = await page.$('.sheet.show input[data-slate="scene"]');
    await scene.click({ clickCount: 3 });
    await scene.type("7B");
    await page.keyboard.press("Tab");
    check(await until(async () => (await get()).slate.scene === "7B", 3000), "slate scene not saved");
    check(await until(async () => (await op("recorder.status")).take.next.startsWith("S7B"), 3000), "the next take is not named by the slate");
    check(await until(() => page.$eval(".sheet.show [data-next-take]", (e) => e.textContent.startsWith("S7B")), 3000), "next take not shown");
    await page.evaluate(() => document.querySelector('.sheet.show input[aria-label="Storage guard"]').click());
    check(await until(async () => (await get()).storage_guard === !before.storage_guard, 3000), "storage guard not saved");
    const gpio = await page.$('.sheet.show input[data-gpio="tally"]');
    await gpio.click({ clickCount: 3 });
    await gpio.type("gpiochip0:7");
    await page.keyboard.press("Tab");
    check(await until(async () => (await get()).gpio.tally === "gpiochip0:7", 3000), "tally line not saved");
    const inputs = (await op("recorder.status")).audio_inputs || [];
    check(await page.$$eval(".sheet.show select.audio-src option", (o) => o.length) >= inputs.length, "audio inputs not offered");
    // server ("the CLI") -> the open sheet
    await op("recorder.settings.set", { settings: { monitor: { guides: "4:3" } } });
    check(await until(() => page.$eval('.sheet.show select[aria-label="Frame guides"]', (e) => e.value === "4:3"), 3000),
          "the open sheet did not follow the other controller");
    await shot(page, "settings-monitoring");
    await page.keyboard.press("Escape");
  } finally {
    await op("recorder.settings.set", { settings: { monitor: { ...MON_OFF, ...before.monitor }, slate: before.slate,
                                                    storage_guard: before.storage_guard, gpio: before.gpio } }).catch((e) => console.log("restore:", e.message));
    await page.close();
  }
});

test("gallery_take_notes_markers_and_csv", ["GAL-13", "REC-15", "REC-11", "REC-09"], async () => {
  const before = await op("recorder.settings.get");
  const st = await op("recorder.status");
  if (before.mode === "h265" && !(st.caps && st.caps.vpu_h265)) await op("recorder.settings.set", { settings: { mode: "raw" } });
  let take = null;
  const page = await newPage();
  try {
    await op("recorder.start");
    // a marker needs the take's timecode (its first frame): try until it has one
    const mark = await until(() => op("recorder.mark", { label: "boom in shot" }).catch(() => null), 10000, 300);
    check(mark && mark.tc, "no marker");
    await sleep(1200);
    take = (await op("recorder.stop")).file.replace(/\.[^.]+$/, "").replace(/_(H265|H264.*|FFV1)$/, "");
    await go(page, "gallery");
    const card = `.take[data-take="${take}"]`;
    check(await until(() => page.$(card), 15000), "new take not listed");
    check(await page.$eval(card, (e) => e.textContent.includes("1")) && await page.$(card + " .thumb .tc"), "start timecode / marker not on the card");
    await page.click(card);
    await page.waitForSelector(".sheet.show .take-edit");
    check(await page.$eval(".sheet.show .markers", (e, tc) => e.textContent.includes(tc) && e.textContent.includes("boom in shot"), mark.tc),
          "marker not listed in the take");
    // good take, then slate + notes - from the web to the server
    check(await clickText(page, ".sheet.show .good-btn", "Mark good"), "no good-take button");
    check(await until(async () => (await op("gallery.get", { take })).meta.good === true, 3000), "good take not saved");
    check(await until(() => page.$(card + " .good-flag"), 3000), "card shows no good flag");
    await page.$eval('.sheet.show [data-k="scene"]', (e) => { e.value = "12A"; e.dispatchEvent(new Event("input")); });
    await page.$eval('.sheet.show [data-k="shot"]', (e) => { e.value = "3"; });
    await page.$eval('.sheet.show [data-k="take_no"]', (e) => { e.value = "4"; });
    await page.$eval('.sheet.show [data-k="notes"]', (e) => { e.value = "focus buzz at the end"; });
    check(await clickText(page, ".sheet.show .take-edit button", "Save"), "no save button");
    const t = await until(async () => { const x = await op("gallery.get", { take }); return x.meta.notes ? x : null; }, 3000);
    check(t && t.slate === "Scene 12A · Shot 3 · Take 4" && t.meta.notes === "focus buzz at the end", "annotate: " + JSON.stringify(t && t.meta));
    check(await until(() => page.$eval(card + " .take-title", (e) => e.textContent === "Scene 12A · Shot 3 · Take 4"), 3000), "card title is not the slate");
    await shot(page, "take-notes");
    await page.keyboard.press("Escape");
    await sleep(400);
    // the editor's sheet: select the take, export a CSV
    check(await clickText(page, ".view-head button", "Select"), "Select button missing");
    await page.click(card);
    await page.evaluate(() => {
      window.__blob = null;
      const make = URL.createObjectURL;
      URL.createObjectURL = (b) => { window.__blob = b; return make(b); };
      HTMLAnchorElement.prototype.click = function () { window.__dl = [this.href, this.download]; };
    });
    check(await clickText(page, ".selbar button", "CSV"), "CSV button missing");
    const csv = await until(() => page.evaluate(async () => (window.__blob ? [window.__dl[1], await window.__blob.text()] : null)), 8000);
    check(csv && csv[0].endsWith(".csv"), "no CSV download: " + JSON.stringify(csv && csv[0]));
    const lines = csv[1].trim().split(/\r?\n/);
    check(lines[0].startsWith("File Name,") && lines[0].includes("Good Take"), "CSV header: " + lines[0]);
    check(lines.some((l) => l.includes(take) && l.includes("focus buzz at the end") && l.includes("12A")), "CSV row: " + lines.slice(1).join(" / "));
    await shot(page, "gallery-csv");
  } finally {
    if ((await op("recorder.status")).recording.active) {
      const f = (await op("recorder.stop").catch(() => ({}))).file;
      take = take || (f && f.replace(/\.[^.]+$/, "").replace(/_(H265|H264.*|FFV1)$/, ""));
    }
    if (take) await op("gallery.delete_take", { take }).catch(() => {});
    await op("recorder.settings.set", { settings: { mode: before.mode, slate: before.slate } }).catch(() => {});
    await page.close();
  }
});

test("terminal_shared_between_browsers", ["TERM-01", "TERM-02", "TERM-04", "TERM-05", "ARC-03"], async () => {
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
  // TERM-05: full screen by the button, out by F11, in by F11, out by the corner button
  const filled = (p) => p.evaluate(() => {
    const w = document.querySelector(".term-wrap.full"), r = w && w.getBoundingClientRect();
    return !!r && r.left <= 0 && r.top <= 0 && r.width >= innerWidth - 1 && r.height >= innerHeight - 1 &&
           getComputedStyle(document.querySelector(".term-tabs")).display !== "none";   // TERM-08: the tabs stay
  });
  check(await clickText(a, ".term-tabs .btn", "Full screen"), "no Full screen button");
  check(await until(() => filled(a), 3000), "the shell does not fill the screen");
  await shot(a, "terminal-full");
  const cols = (await op("term.list")).terminals.find((x) => x.term === id).cols;
  await a.keyboard.press("F11");
  check(await until(() => a.evaluate(() => !document.querySelector(".term-wrap.full")), 3000), "F11 did not leave full screen");
  await a.keyboard.press("F11");
  check(await until(() => filled(a), 3000), "F11 did not enter full screen");
  await a.click(".term-exitfull");
  check(await until(() => a.evaluate(() => !document.querySelector(".term-wrap.full")), 3000), "the corner button did not leave");
  await a.click(".term-box .xterm");
  await a.keyboard.type("echo AFTER_$((1+1))\n");          // F11 never reached the shell: the line is clean
  check(await until(() => a.$eval(".term-box .xterm-rows", (e) => /AFTER_2/.test(e.textContent)), 8000), "shell broken after F11");
  check(typeof cols === "number", "term.list has no size");
  await op("term.close", { term: id }).catch(() => {});
  await a.close();
  await b.close();
});

test("terminal_tabs_and_splits", ["TERM-08", "TERM-05", "TERM-02"], async () => {
  // like Tilix: a tab holds panes split right / down, dividers drag, keys split / move / maximize,
  // and the tab strip (+, the splits) stays in full screen. The layout is this page's own.
  const a = await newPage();
  const opened = [];
  try {
    await go(a, "terminal");
    await a.waitForSelector(".term-tabs .btn");
    await clickText(a, ".term-tabs .btn, .term-box .btn", "shell");
    await a.waitForSelector(".term-box .xterm", { timeout: 10000 });
    const panes = () => a.evaluate(() => [...document.querySelectorAll(".term-box .term-pane")].map((p) => {
      const r = p.getBoundingClientRect();
      return { term: +p.dataset.term, w: r.width, h: r.height, focus: p.classList.contains("focus") };
    }));
    await a.evaluate(() => document.querySelector('.term-split[data-split="row"]').click());
    check(await until(async () => (await panes()).length === 2, 8000), "split right made no second pane");
    await a.keyboard.down("Control"); await a.keyboard.down("Alt"); await a.keyboard.press("KeyD");
    await a.keyboard.up("Alt"); await a.keyboard.up("Control");
    check(await until(async () => (await panes()).length === 3, 8000), "Ctrl+Alt+D made no third pane");
    let p = await panes();
    opened.push(...p.map((x) => x.term));
    check(p[1].w > 100 && Math.abs(p[1].h - p[2].h) < 4 && p[0].h > p[1].h * 1.8, "panes not laid out right + down: " + JSON.stringify(p));
    const listed = (await op("term.list")).terminals.map((t) => t.term);
    check(p.every((x) => listed.includes(x.term)), "a pane's shell is not on the server");
    await a.keyboard.type("echo SPLIT_$((6*7))\n");
    check(await until(() => a.evaluate(() => /SPLIT_42/.test(document.querySelector(".term-pane.focus")?.textContent || "")), 8000),
          "the focused pane did not get the typing");
    // drag the vertical divider (away from the junction with the right column's divider)
    const d = await a.evaluate(() => { const r = document.querySelector(".term-split.sp-row > .term-div").getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 4]; });
    await a.mouse.move(d[0], d[1]); await a.mouse.down(); await a.mouse.move(d[0] - 150, d[1], { steps: 8 }); await a.mouse.up();
    check(await until(async () => (await panes())[0].w < p[0].w - 100, 3000), "dragging the divider did not resize the panes");
    await a.keyboard.down("Alt"); await a.keyboard.press("ArrowLeft"); await a.keyboard.up("Alt");
    check(await until(async () => (await panes())[0].focus, 2000), "Alt+Left did not move to the left pane");
    await a.keyboard.press("F11");
    check(await until(() => a.evaluate(() => {
      const w = document.querySelector(".term-wrap.full"), t = w && w.querySelector(".term-tabs");
      return !!t && getComputedStyle(t).display !== "none" && !!w.querySelector(".term-new") && !!w.querySelector(".term-split");
    }), 3000), "full screen hides the tabs, + or the splits");
    await a.keyboard.down("Control"); await a.keyboard.down("Shift"); await a.keyboard.press("KeyX");
    await a.keyboard.up("Shift"); await a.keyboard.up("Control");
    check(await until(async () => (await panes()).length === 1, 2000), "Ctrl+Shift+X did not maximize the pane");
    await a.keyboard.down("Control"); await a.keyboard.down("Shift"); await a.keyboard.press("KeyX");
    await a.keyboard.up("Shift"); await a.keyboard.up("Control");
    check(await until(async () => (await panes()).length === 3, 2000), "Ctrl+Shift+X did not restore the panes");
    const tabs0 = await a.evaluate(() => document.querySelectorAll(".term-tab").length);
    await a.evaluate(() => document.querySelector(".term-wrap.full .term-new").click());
    check(await until(() => a.evaluate((n) => document.querySelectorAll(".term-tab").length === n + 1, tabs0), 8000), "+ in full screen made no tab");
    p = await panes();
    opened.push(...p.map((x) => x.term));
    check(p.length === 1, "a new tab is not one pane");
    await shot(a, "terminal-splits");
    await a.keyboard.press("F11");
  } finally {
    for (const id of new Set(opened)) await op("term.close", { term: id }).catch(() => {});
    await a.close();
  }
});

test("terminal_drag_and_drop", ["TERM-09", "TERM-08"], async () => {
  // a pane dragged by its bar lands on another pane's half, or on the strip as a tab of its own;
  // a tab dragged along the strip reorders; a one-shell tab dropped on a pane joins it
  const a = await newPage();
  const opened = new Set();
  try {
    await go(a, "terminal");
    await a.waitForSelector(".term-tabs .btn");
    await clickText(a, ".term-tabs .btn, .term-box .btn", "shell");
    await a.waitForSelector(".term-box .xterm", { timeout: 10000 });
    const panes = () => a.evaluate(() => [...document.querySelectorAll(".term-box .term-pane")].map((p) => {
      const r = p.getBoundingClientRect();
      return { term: +p.dataset.term, x: r.x, y: r.y, w: r.width, h: r.height };
    }));
    const tabKeys = () => a.evaluate(() => [...document.querySelectorAll(".term-tab")].map((t) => +t.dataset.key));
    const rectOf = (sel) => a.evaluate((s) => { const r = document.querySelector(s).getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; }, sel);
    const drag = async (from, to) => {
      await a.mouse.move(from[0], from[1]); await a.mouse.down();
      await a.mouse.move(to[0], to[1], { steps: 12 }); await sleep(120); await a.mouse.up(); await sleep(400);
    };
    await a.evaluate(() => document.querySelector('.term-split[data-split="row"]').click());
    check(await until(async () => (await panes()).length === 2, 8000), "no second pane");
    await a.evaluate(() => document.querySelector('.term-split[data-split="col"]').click());
    check(await until(async () => (await panes()).length === 3, 8000), "no third pane");
    let p = await panes();
    p.forEach((x) => opened.add(x.term));
    const [p0, , p2] = p;
    // p2 (bottom right) by its bar onto the left half of p0: p2 | p0 | p1
    const bar = await rectOf(`.term-pane[data-term="${p2.term}"] .term-pane-bar .name`);
    await drag([bar.x + bar.w / 2, bar.y + bar.h / 2], [p0.x + 40, p0.y + p0.h / 2]);
    p = await panes();
    const at = (id) => p.find((x) => x.term === id);
    check(p.length === 3 && at(p2.term).x < at(p0.term).x && Math.abs(at(p2.term).h - at(p0.term).h) < 4,
          "the dragged shell did not land left of the target: " + JSON.stringify(p));
    // the same pane onto the strip's free space: a tab of its own
    const tabs0 = await tabKeys();
    const bar2 = await rectOf(`.term-pane[data-term="${p2.term}"] .term-pane-bar .name`);
    const plus = await rectOf(".term-new");
    await drag([bar2.x + bar2.w / 2, bar2.y + bar2.h / 2], [plus.x + plus.w + 160, plus.y + plus.h / 2]);
    const tabs1 = await tabKeys();
    check(tabs1.length === tabs0.length + 1 && (await panes()).length === 1 && (await panes())[0].term === p2.term,
          "the pane dropped on the strip did not become its own tab");
    // reorder: the last tab before the first
    const first = await rectOf(".term-tab");
    const last = await a.evaluate(() => { const r = [...document.querySelectorAll(".term-tab")].pop().getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
    await drag([last.x + last.w / 2, last.y + last.h / 2], [first.x + 6, first.y + first.h / 2]);
    const tabs2 = await tabKeys();
    check(tabs2[0] === tabs1[tabs1.length - 1], "dragging a tab did not reorder the strip: " + JSON.stringify([tabs1, tabs2]));
    // the one-shell tab (now first) onto the right half of a pane of the other tab: it joins it
    await a.evaluate(() => document.querySelectorAll(".term-tab")[1].click());
    await sleep(400);
    const host = (await panes())[0];
    const t0 = await rectOf(".term-tab");
    await drag([t0.x + t0.w / 2, t0.y + t0.h / 2], [host.x + host.w - 30, host.y + host.h / 2]);
    check((await tabKeys()).length === tabs2.length - 1 && (await panes()).some((x) => x.term === p2.term),
          "the one-shell tab did not join the pane it was dropped on");
    // a drag cancelled with Esc changes nothing
    const before = JSON.stringify(await panes());
    const b3 = await rectOf(".term-pane-bar .name");
    await a.mouse.move(b3.x + 5, b3.y + 5); await a.mouse.down(); await a.mouse.move(b3.x + 300, b3.y + 300, { steps: 8 });
    await a.keyboard.press("Escape"); await a.mouse.up(); await sleep(300);
    check(JSON.stringify(await panes()) === before, "Esc did not cancel the drag");
    await shot(a, "terminal-dnd");
  } finally {
    for (const id of opened) await op("term.close", { term: id }).catch(() => {});
    await a.close();
  }
});

test("remote_screen_is_live_and_the_mouse_lands_on_the_same_spot", ["SCR-01", "SCR-02", "SCR-03", "SCR-05"], async () => {
  const p0 = await op("in.pointer").catch(() => null);
  const page = await newPage();
  try {
    await go(page, "screen");
    check(await until(() => page.$(".screen-stage.playing"), 20000), "no live picture of the desktop within 20 s");
    const st = await op("screen.status");
    check(st.state === "live" && st.viewers >= 1 && st.screen && st.screen[0] > 0, st);
    const [sw, sh] = st.screen;
    // hover (no click: nothing on the Pi's desktop is touched) at 30 % / 60 % of the picture
    const box = await (await page.$(".screen-stage")).boundingBox();
    const k = Math.min(box.width / sw, box.height / sh);
    const x0 = box.x + (box.width - sw * k) / 2, y0 = box.y + (box.height - sh * k) / 2;
    const want = [Math.round(sw * 0.3), Math.round(sh * 0.6)];
    await page.mouse.move(x0 + sw * k * 0.3, y0 + sh * k * 0.6, { steps: 4 });
    const got = await until(async () => {
      const p = await op("in.pointer");
      return Math.abs(p.x - want[0]) <= 3 && Math.abs(p.y - want[1]) <= 3 && p;
    }, 3000);
    check(got, `pointer should be near ${want}, is ${JSON.stringify(await op("in.pointer"))}`);
    await shot(page, "screen");
    // Stream sheet (SCR-05): encoder + bitrate, showing what runs now
    const btn = await page.$$eval(".screen-bar button", (bs) => bs.findIndex((b) => b.textContent.includes("Stream")));
    check(btn >= 0, "no Stream button");
    await (await page.$$(".screen-bar button"))[btn].click();
    check(await until(() => page.$(".sheet.show select"), 3000), "Stream sheet has no bitrate");
    const txt = await page.$eval(".sheet.show", (e) => e.textContent);
    check(/VPU/.test(txt) && /CPU/.test(txt) && /Now:/.test(txt), txt);
  } finally {
    await page.close();
    if (p0) await op("in.move_to", { x: p0.x, y: p0.y }).catch(() => {});
  }
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
