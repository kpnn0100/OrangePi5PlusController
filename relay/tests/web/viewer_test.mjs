#!/usr/bin/env node
// The browser viewer of the device manager, end to end in Chrome (requirement-tagged):
// device list -> access key -> ports -> B/d/<id>/<port>/ -> Arstro Remote's web UI through the
// encrypted channel (service worker for HTTP, WebSocket shim, cookie jar).
//
//   RELAY_URL=http://localhost:8090/arstro/internal/devices DEVICE_ID=<id> DEVICE_KEY=ak1_... \
//   DEVICE_PORT=<Arstro web port on the device> ARSTRO_TOKEN=<its web password> \
//   [RELAY_PASSWORD=...] node relay/tests/web/viewer_test.mjs [-k name] [--list] [--shots DIR]
//
// RELAY_URL must be a secure context for the browser: https://..., or http://localhost.
// A run makes two wrong-key attempts on purpose; the device refuses new viewers for a minute
// after more than five in 60 s, so leave a minute between runs.
// puppeteer-core comes from server/tests/web/node_modules (cd server/tests/web && npm install).
// Optional: CHROME=/path/to/chrome.

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..", "..");
const require = createRequire(path.join(ROOT, "server", "tests", "web", "package.json"));
const puppeteer = require("puppeteer-core");

const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const ONLY = opt("-k");
const SHOTS = opt("--shots");
const RELAY = (process.env.RELAY_URL || "").replace(/\/+$/, "");
const BASE = RELAY + "/";
const B = RELAY ? new URL(BASE).pathname.replace(/\/$/, "") : "";
const ID = process.env.DEVICE_ID || "";
const KEY = process.env.DEVICE_KEY || "";
const PORT = Number(process.env.DEVICE_PORT || 0);
const TOKEN = process.env.ARSTRO_TOKEN || "";
const PASSWORD = process.env.RELAY_PASSWORD || "";
const DEVICE_PAGE = RELAY + "/d/" + ID + "/" + PORT + "/";

const TESTS = [];
const test = (name, reqs, fn) => TESTS.push({ name, reqs, fn });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function check(cond, msg) { if (!cond) throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg)); }
async function until(fn, timeout = 10000, every = 150) {
  const end = Date.now() + timeout;
  for (;;) {
    const v = await fn().catch(() => null);
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(every);
  }
}

function findChrome() {
  const c = [process.env.CHROME, "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium",
             "/usr/bin/chromium-browser", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"];
  return c.find((p) => p && fs.existsSync(p));
}

function randomKey() {
  return "ak1_" + Buffer.from(crypto.getRandomValues(new Uint8Array(32))).toString("base64url");
}

async function relayApi(p) {
  const r = await fetch(RELAY + "/api/" + p, { headers: PASSWORD ? { Authorization: "Bearer " + PASSWORD } : {} });
  if (!r.ok) throw new Error(`${p}: HTTP ${r.status}`);
  return r.json();
}

// ------------------------------------------------------------------------------------ browser
let browser;
let ctx;                    // one incognito context for the main flow (key stored once)
const pageErrors = [];

function watch(page) {
  page.on("pageerror", (e) => pageErrors.push(e.message));
  return page;
}

async function newPage(c = ctx, { width = 1280, height = 800, mobile = false } = {}) {
  const page = watch(await c.newPage());
  await page.setViewport({ width, height, isMobile: mobile, hasTouch: mobile });
  return page;
}

async function shot(page, name) {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name + ".png") });
}

async function openList(page) {
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#devices, #login-form, .empty", { timeout: 10000 });
  if (await page.$("#login-form")) {
    check(PASSWORD, "the device manager asks for a password: set RELAY_PASSWORD");
    await page.type("#login-form input[type=password]", PASSWORD);
    await page.click("#login-form button[type=submit]");
    await page.waitForSelector("#devices", { timeout: 10000 });
  }
}

async function openDevice(page) {
  await openList(page);
  await page.waitForSelector(`.device[data-id="${ID}"]`, { timeout: 15000 });
  await page.click(`.device[data-id="${ID}"]`);
  await page.waitForSelector("#check", { timeout: 5000 });
  await page.waitForSelector("#key, #key-stored", { timeout: 5000 });
}

const text = (page, sel) => page.$eval(sel, (e) => e.textContent).catch(() => "");

/** Arstro Remote's UI in `page` (already on the device page): sign in if asked, wait for a view
 *  (views mount only after the UI's WebSocket said hello). */
async function arstroSignedIn(page) {
  await page.waitForSelector(".login-card, .view", { timeout: 20000 });
  if (await page.$(".login-card")) {
    check(TOKEN, "Arstro Remote asks for its password: set ARSTRO_TOKEN");
    await page.type(".login-card input[type=password]", TOKEN);
    await page.click(".login-card button[type=submit]");
  }
  await page.waitForSelector(".view", { timeout: 20000 });
}

// --------------------------------------------------------------------------------------- tests
test("list_shows_the_device_online", ["RLY-21", "RLY-22"], async () => {
  const page = await newPage();
  await openList(page);
  const row = await page.waitForSelector(`.device[data-id="${ID}"]`, { timeout: 15000 });
  check(row, "device not listed");
  check(await until(() => page.$eval(`.device[data-id="${ID}"] .pill`, (e) => e.classList.contains("on"))), "device not online");
  const meta = await text(page, `.device[data-id="${ID}"] .meta`);
  const dev = (await relayApi("devices")).devices.find((d) => d.id === ID);
  if (dev.hostname) check(meta.includes(dev.hostname), "hostname not shown: " + meta);
  check(/online/.test(meta), "online time not shown: " + meta);
  check(/Agent \/ CLI access/.test(await text(page, ".foot")), "agent note missing");
  check(await page.$eval("#client-link", (a) => a.getAttribute("href").endsWith("/client/arstro_relay_client.py")), "client link");
  await shot(page, "list");
  await page.close();
});

test("list_fits_a_phone", ["RLY-22"], async () => {
  const page = await newPage(ctx, { width: 360, height: 740, mobile: true });
  await openDevice(page);
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(over <= 0, `horizontal overflow of ${over}px at 360px`);
  await shot(page, "device-phone");
  await page.close();
});

test("wrong_key_is_refused", ["RLY-22", "RLY-03"], async () => {
  const page = await newPage();
  await openDevice(page);
  check(await page.$("#key"), "a key is already stored in a fresh browser context");
  await page.type("#key", randomKey());
  await page.click("#check");
  check(await until(() => text(page, "#device-error").then((t) => /wrong access key/.test(t)), 15000),
        "wrong key not reported: " + (await text(page, "#device-error")));
  check(!(await page.$("#key-stored")), "a wrong key was stored");
  await page.$eval("#key", (e) => { e.value = ""; });
  await page.type("#key", "ak1_tooshort");
  await page.click("#check");
  check(await until(() => text(page, "#device-error").then((t) => /too short or too long/.test(t)), 3000), "bad key format not reported");
  await page.close();
});

test("key_check_lists_ports_and_prefills_the_web_port", ["RLY-22", "RLY-04"], async () => {
  const page = await newPage();
  await openDevice(page);
  const dev = (await relayApi("devices")).devices.find((d) => d.id === ID);
  if (dev.web) check(await page.$eval("#port", (e) => e.value) === String(dev.web), "port not prefilled with the web hint");
  await page.type("#key", "  " + KEY.slice(0, 20) + "\n" + KEY.slice(20) + " ");   // pasted with whitespace
  await page.click("#remember");
  await page.click("#check");
  const btn = await until(() => page.$(`.port-btn[data-port="${PORT}"]`), 15000);
  check(btn, "port not listed: " + (await text(page, "#device-error")) + " " + (await text(page, "#ports")));
  check(await page.waitForSelector("#key-stored", { timeout: 5000 }), "key not stored after a good check");
  check(/remembered/.test(await text(page, "#key-stored")), "remember not shown");
  // stored as a non-extractable CryptoKey, never as text
  const rec = await page.evaluate(() => new Promise((resolve, reject) => {
    const r = indexedDB.open("arstro-devices");
    r.onsuccess = () => {
      const g = r.result.transaction("keys").objectStore("keys").getAll();
      g.onsuccess = () => resolve(g.result.map((x) => ({ id: x.id, key_id: x.key_id, remember: x.remember, expires: x.expires,
                                                         type: x.key && x.key.constructor.name, extractable: x.key && x.key.extractable,
                                                         algo: x.key && x.key.algorithm.name, json: JSON.stringify(x) })));
      g.onerror = () => reject(g.error);
    };
    r.onerror = () => reject(r.error);
  }));
  const mine = rec.find((x) => x.id === ID);
  check(mine && mine.type === "CryptoKey" && mine.extractable === false && mine.algo === "HKDF", "stored key: " + JSON.stringify(mine));
  check(mine.remember === true && mine.expires === null, "remember flag");
  check(!rec.some((x) => x.json.includes(KEY.slice(4, 20))), "the key text is stored");
  await page.click(`.port-btn[data-port="${PORT}"]`);
  check(await page.$eval("#port", (e) => e.value) === String(PORT), "port button does not fill the port");
  await shot(page, "device-ports");
  await page.close();
});

let appPage = null;

test("open_runs_arstro_remote_through_the_viewer", ["RLY-22"], async () => {
  const page = await newPage();
  await openDevice(page);
  check(await page.$("#key-stored"), "the key of the previous test is not stored");
  await page.$eval("#port", (e) => { e.value = ""; });
  await page.type("#port", String(PORT));
  const opened = ctx.waitForTarget((t) => t.url().startsWith(DEVICE_PAGE), { timeout: 20000 });
  await page.click("#open");
  const target = await opened;
  appPage = watch(await target.page());
  await appPage.setViewport({ width: 1280, height: 800 });
  await arstroSignedIn(appPage);
  check(appPage.url().startsWith(DEVICE_PAGE), "left the device page: " + appPage.url());
  const shim = await appPage.evaluate(() => ({
    shim: !!window.__arstroShim, tag: !!document.querySelector('script[src$="/shim.js"]'),
    sw: !!(navigator.serviceWorker && navigator.serviceWorker.controller),
  }));
  check(shim.shim && shim.tag && shim.sw, "viewer not active in the page: " + JSON.stringify(shim));
  const ping = await appPage.evaluate(() => fetch("/api/ping", { cache: "no-store" }).then((r) => r.json()));
  check(ping.authorized === true, "/api/ping through the page: " + JSON.stringify(ping));
  const hostOk = ping.hostname ? await until(() => appPage.evaluate((h) => document.body.textContent.includes(h), ping.hostname), 5000) : true;
  check(hostOk, "the UI does not show the device's host name");
  await shot(appPage, "arstro");
  await page.close();
});

test("arstro_websocket_api_through_the_shim", ["RLY-22"], async () => {
  check(appPage, "needs open_runs_arstro_remote_through_the_viewer");
  const r = await appPage.evaluate(async () => {
    const out = { states: [] };
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    out.isShim = ws instanceof WebSocket && !!window.__arstroNativeWebSocket && !(ws instanceof window.__arstroNativeWebSocket);
    out.consts = [WebSocket.CONNECTING, WebSocket.OPEN, WebSocket.CLOSING, WebSocket.CLOSED, ws.OPEN];
    out.url = ws.url;
    out.states.push(ws.readyState);
    ws.binaryType = "arraybuffer";
    try { ws.send("x"); out.sendWhileConnecting = "no error"; } catch (e) { out.sendWhileConnecting = e.name; }
    const events = [];
    ws.addEventListener("open", () => events.push("open"));
    await new Promise((resolve, reject) => {
      ws.onopen = () => resolve();
      ws.onerror = () => reject(new Error("error event"));
      setTimeout(() => reject(new Error("no open")), 20000);
    });
    out.states.push(ws.readyState);
    // Arstro's protocol: JSON ops as text, answers as binary [type u8][len u32 BE][json]
    const reply = new Promise((resolve, reject) => {
      ws.onmessage = (e) => {
        if (!(e.data instanceof ArrayBuffer)) return;
        const b = new Uint8Array(e.data);
        if (b[0] !== 1) return;
        const msg = JSON.parse(new TextDecoder().decode(b.subarray(5, 5 + ((b[1] << 24) | (b[2] << 16) | (b[3] << 8) | b[4]))));
        if (msg.id === 7) resolve(msg);
      };
      setTimeout(() => reject(new Error("no answer to hello")), 15000);
    });
    ws.send(JSON.stringify({ op: "hello", id: 7, app: "viewer-test", version: "test" }));
    out.buffered = typeof ws.bufferedAmount;
    const msg = await reply;
    out.hello = { ok: msg.ok, hostname: msg.data && msg.data.hostname };
    out.events = events;
    const closed = new Promise((resolve) => { ws.onclose = (e) => resolve({ code: e.code, clean: e.wasClean, type: e.constructor.name }); });
    try { ws.close(1001); out.badCode = "no error"; } catch (e) { out.badCode = e.name; }
    ws.close(1000, "bye");
    out.states.push(ws.readyState);
    out.close = await closed;
    out.states.push(ws.readyState);
    return out;
  });
  check(r.isShim, "WebSocket is not the shim: " + JSON.stringify(r));
  check(JSON.stringify(r.consts) === "[0,1,2,3,1]", "constants " + r.consts);
  check(r.url.endsWith("/ws") && /^wss?:/.test(r.url), "url " + r.url);
  check(r.sendWhileConnecting === "InvalidStateError", "send while connecting: " + r.sendWhileConnecting);
  check(r.badCode === "InvalidAccessError", "close(1001): " + r.badCode);
  check(r.hello.ok, "hello over the shim failed: " + JSON.stringify(r.hello));
  check(JSON.stringify(r.states) === "[0,1,2,3]", "readyState sequence " + r.states);
  check(r.close.clean && r.close.code === 1000 && r.close.type === "CloseEvent", "close event " + JSON.stringify(r.close));
  check(r.events.includes("open"), "addEventListener('open')");
});

test("status_errors_and_bodies_pass_through", ["RLY-22"], async () => {
  check(appPage, "needs open_runs_arstro_remote_through_the_viewer");
  const r = await appPage.evaluate(async () => {
    const nf = await fetch("/api/no-such-route");
    const css = await fetch("/assets/app.css");
    const body = "x".repeat(300000);
    const big = await fetch("/api/op/no.such.op", { method: "POST", headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ pad: body }) });
    const head = await fetch("/api/ping", { method: "HEAD" });
    return { nf: nf.status, css: [css.status, css.headers.get("content-type"), (await css.text()).length],
             big: [big.status, (await big.text()).slice(0, 200)], head: head.status };
  });
  check(r.nf === 404, "404 not passed through: " + r.nf);
  check(r.css[0] === 200 && /css/.test(r.css[1]) && r.css[2] > 1000, "asset: " + r.css);
  check(r.big[0] !== 502 && r.big[0] < 500, "POST with a 300 KB body: " + r.big);
  check(r.head < 500, "HEAD: " + r.head);
});

test("big_upload_and_download_through_the_page", ["RLY-22", "RLY-04"], async () => {
  check(appPage, "needs open_runs_arstro_remote_through_the_viewer");
  // as the Files view does it: XHR PUT of a File (the device's server requires Content-Length),
  // then the download link - 3 MB both ways, through the window / ack flow control
  const r = await appPage.evaluate(async () => {
    const op = (name, args) => fetch("/api/op/" + name, { method: "POST", headers: { "Content-Type": "application/json" },
                                                          body: JSON.stringify(args || {}) }).then((x) => x.json());
    const roots = await op("files.roots");
    const dir = roots.data.roots[0].path;
    const size = 3 * 1024 * 1024 + 12345;
    const data = new Uint8Array(size);
    for (let i = 0; i < size; i += 65536) crypto.getRandomValues(data.subarray(i, Math.min(i + 65536, size)));
    const name = "viewer-test-" + Date.now() + ".bin";
    const file = new File([data], name);
    const up = await new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `/api/files/upload?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}&overwrite=1`);
      xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText.slice(0, 300) });
      xhr.onerror = () => resolve({ status: 0, text: "xhr error" });
      xhr.send(file);
    });
    const hex = async (buf) => Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", buf)), (b) => b.toString(16).padStart(2, "0")).join("");
    const sent = await hex(data);
    const path = dir.replace(/\/$/, "") + "/" + name;
    const dl = await fetch("/api/files/download?path=" + encodeURIComponent(path));
    const got = new Uint8Array(await dl.arrayBuffer());
    const ranged = await fetch("/api/files/download?path=" + encodeURIComponent(path), { headers: { Range: "bytes=100-199" } });
    const part = new Uint8Array(await ranged.arrayBuffer());
    await op("files.delete", { path });
    return { up, size, dlStatus: dl.status, gotSize: got.length, same: sent === await hex(got),
             range: [ranged.status, part.length, ranged.status === 206 ? part.every((b, i) => b === data[100 + i]) : null] };
  });
  check(r.up.status === 200 && /"ok":\s*true/.test(r.up.text), "upload: " + JSON.stringify(r.up));
  check(r.dlStatus === 200 && r.gotSize === r.size && r.same, `download: ${r.dlStatus} ${r.gotSize}/${r.size} same=${r.same}`);
  check(r.range[0] === 200 || (r.range[0] === 206 && r.range[1] === 100 && r.range[2]), "Range request: " + r.range);
});

test("links_and_iframes_are_rewritten_into_the_device", ["RLY-22"], async () => {
  check(appPage, "needs open_runs_arstro_remote_through_the_viewer");
  const prefix = new URL(DEVICE_PAGE).pathname.replace(/\/$/, "");
  const r = await appPage.evaluate(async () => {
    const f = document.createElement("iframe");
    f.setAttribute("src", "/api/ping");
    f.style.display = "none";
    const g = document.createElement("iframe");
    g.src = "/assets/app.css";
    document.body.append(f, g);
    await new Promise((res) => f.addEventListener("load", res, { once: true }));
    let pingText = "";
    try { pingText = f.contentDocument.body.textContent; } catch (e) { pingText = "cross-origin? " + e.message; }
    const a = document.createElement("a");
    a.href = "/files?x=1";
    document.body.append(a);
    document.addEventListener("click", (e) => e.preventDefault(), { once: true });
    a.click();
    const out = { f: f.getAttribute("src"), g: g.getAttribute("src"), a: a.getAttribute("href"), pingText };
    f.remove(); g.remove(); a.remove();
    return out;
  });
  check(r.f === prefix + "/api/ping", "iframe setAttribute: " + r.f);
  check(r.g === prefix + "/assets/app.css", "iframe src: " + r.g);
  check(r.a === prefix + "/files?x=1", "link: " + r.a);
  check(/authorized/.test(r.pingText), "iframe content did not come from the device: " + r.pingText);
});

test("reload_keeps_the_app_login_in_the_cookie_jar", ["RLY-22"], async () => {
  check(appPage, "needs open_runs_arstro_remote_through_the_viewer");
  await appPage.reload({ waitUntil: "domcontentloaded" });
  await appPage.waitForSelector(".login-card, .view", { timeout: 20000 });
  check(!(await appPage.$(".login-card")), "Arstro asked for its password again after a reload");
  await appPage.waitForSelector(".view", { timeout: 20000 });
  const jar = await appPage.evaluate((id) => new Promise((resolve) => {
    const r = indexedDB.open("arstro-devices");
    r.onsuccess = () => {
      const g = r.result.transaction("cookies").objectStore("cookies").getAllKeys();
      g.onsuccess = () => resolve(g.result.filter((k) => String(k).startsWith(id + ":")));
    };
  }), ID);
  check(jar.includes(ID + ":" + PORT), "cookie jar entry missing: " + jar);
  check(!(await appPage.evaluate(() => document.cookie)).includes("arstro_token"), "the app's cookie leaked into real cookies");
});

test("a_port_without_a_server_explains_why", ["RLY-22", "RLY-04"], async () => {
  const page = await newPage();
  const ports = new Set(((await relayApi("devices")).devices.find((d) => d.id === ID) || {}).ports || []);
  let dead = 1;                                   // tcpmux: nothing listens there on a normal machine
  while (ports.has(dead)) dead++;
  await page.goto(RELAY + "/d/" + ID + "/" + dead + "/", { waitUntil: "domcontentloaded" });
  check(await until(() => text(page, "#error").then((t) => t.length > 0), 15000), "no explanation shown");
  const err = await text(page, "#error");
  check(/port|listen|refused|not allowed/i.test(err), "unclear explanation: " + err);
  check(await page.$("#actions:not([hidden])"), "no retry button");
  await page.close();
});

test("boot_page_asks_for_the_key_and_refuses_a_wrong_one", ["RLY-22", "RLY-03"], async () => {
  const fresh = await browser.createBrowserContext();
  try {
    const page = await newPage(fresh);
    if (PASSWORD) { await openList(page); }
    await page.goto(DEVICE_PAGE, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#key-form:not([hidden])", { timeout: 15000 });
    await page.type("#key", randomKey());
    await page.click("#key-submit");
    check(await until(() => text(page, "#error").then((t) => /wrong access key/.test(t)), 15000),
          "wrong key not refused on the boot page: " + (await text(page, "#error")));
    await page.$eval("#key", (e) => { e.value = ""; });
    await page.type("#key", KEY);
    await Promise.all([page.waitForNavigation({ timeout: 20000 }).catch(() => null), page.click("#key-submit")]);
    await arstroSignedIn(page);
    check(page.url().startsWith(DEVICE_PAGE), "not on the device page: " + page.url());
    await shot(page, "boot-then-arstro");
  } finally {
    await fresh.close();
  }
});

test("forget_removes_the_key", ["RLY-22"], async () => {
  if (appPage) { await appPage.close(); appPage = null; }
  const page = await newPage();
  await openDevice(page);
  await page.waitForSelector("#forget", { timeout: 5000 });
  await page.click("#forget");
  await page.waitForSelector("#key", { timeout: 5000 });
  check(/forgotten/.test(await text(page, "#device-ok")), "no confirmation");
  const left = await page.evaluate((id) => new Promise((resolve) => {
    const r = indexedDB.open("arstro-devices");
    r.onsuccess = () => {
      const g = r.result.transaction("keys").objectStore("keys").get(id);
      g.onsuccess = () => resolve(!!g.result);
    };
  }), ID);
  check(!left, "the key is still stored");
  // without the key, the device's pages are refused (401) and navigations get the boot page
  const st = await page.evaluate((u) => fetch(u + "api/ping").then((r) => r.status), DEVICE_PAGE);
  check(st === 401, "a request without a key: " + st);
  await page.close();
});

// ------------------------------------------------------------------------------------ runner
async function main() {
  if (args.includes("--list")) { for (const t of TESTS) console.log(t.name.padEnd(52), t.reqs.join(" ")); return 0; }
  if (!RELAY.startsWith("http") || !ID || !KEY || !PORT) {
    console.error("set RELAY_URL, DEVICE_ID, DEVICE_KEY, DEVICE_PORT (and ARSTRO_TOKEN, RELAY_PASSWORD if needed)");
    return 2;
  }
  const exe = findChrome();
  if (!exe) { console.error("Chrome not found - set CHROME=/path/to/chrome"); return 2; }
  if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
  browser = await puppeteer.launch({ executablePath: exe, headless: true, args: ["--no-sandbox"] });
  ctx = await browser.createBrowserContext();
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
        console.log(`FAIL  ${t.name.padEnd(50)} ${e.message}`);
      }
      if (ok) { passed++; console.log(`PASS  ${t.name.padEnd(50)} ${((Date.now() - t0) / 1000).toFixed(1).padStart(5)}s  ${t.reqs.join(" ")}`); }
      else failed++;
      for (const r of t.reqs) (covered[r] = covered[r] || []).push(ok);
    }
  } finally {
    await browser.close();
  }
  console.log("\nrequirements: " + Object.keys(covered).sort().map((r) => r + (covered[r].every(Boolean) ? "" : "(FAIL)")).join(" "));
  console.log(`${passed} passed, ${failed} failed`);
  return failed ? 1 : 0;
}

process.exit(await main());
