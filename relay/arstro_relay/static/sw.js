/* The browser viewer's service worker (RLY-22). Registered as B/sw.js with scope B/ (the relay
 * sends Service-Worker-Allowed: B/). It turns the pages of B/d/<id>/<port>/... into HTTP
 * requests to 127.0.0.1:<port> on device <id>, through the end-to-end encrypted channel.
 *
 * Routing (docs/relay/PROTOCOL.md section 5), for every fetch:
 *   1. path under B/d/<id>/<port>/  -> device <id>, port, path = the rest + query
 *   2. a same-origin URL outside B/ whose requesting page (the fetch's client, or the referrer
 *      for navigations) is under B/d/<id>/<port>/  -> that device and port, path = path + query
 *   3. else the network
 * One channel per device while the worker lives (reopened when it fails): every request AND
 * every WebSocket of the device's pages goes through it (the shim hands its WebSockets over with
 * a MessagePort each - see "WebSockets" below), so a browser keeps one connection (or one pipe,
 * PROTOCOL.md section 6) per device. Request bodies go out in CHUNK pieces (with their
 * Content-Length), responses come back as streams with backpressure (ack on pull).
 * Transport: ArstroE2E.transport, "auto" unless the worker was registered as sw.js?transport=ws|http.
 * Set-Cookie goes into the cookie jar (IndexedDB), the jar's cookies go out as Cookie; HTML gets
 * <script src="B/shim.js"></script> right after <head>; Location is rewritten into B/d/....
 * No key for the device: a navigation gets the boot page (network), other requests 401.
 */
"use strict";

importScripts("static/e2e.js", "static/viewer.js");

const E = self.ArstroE2E;
const V = self.ArstroViewer;
const B = V.baseOf(self.location.pathname, "sw.js");
const ORIGIN = self.location.origin;
const SHIM = B + "/shim.js";
const RETRY_AFTER_FAILURE = 2000;   // a channel that failed to open is not retried sooner
const FORCED = new URL(self.location.href).searchParams.get("transport");
if (FORCED === "ws" || FORCED === "http" || FORCED === "auto") E.transport = FORCED;

const channels = new Map();          // device id -> {key_id, promise, ch, error, failedAt}
const lastError = new Map();         // "<id>:<port>" -> why the last navigation failed (for the boot page)

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  let url;
  try { url = new URL(event.request.url); } catch (e) { return; }
  if (url.origin !== ORIGIN) return;                                  // rule 3
  if (V.parseDevicePath(url.pathname, B)) {                           // rule 1
    event.respondWith(handle(event, V.route(url, B, ORIGIN, null)));
    return;
  }
  if (V.inBase(url.pathname, B)) return;                              // the device manager itself
  event.respondWith(viaClient(event, url));                           // rule 2 (or 3)
});

self.addEventListener("message", (event) => {
  const m = event.data || {};
  const reply = event.ports && event.ports[0];
  if (m.t === "why") {
    if (reply) reply.postMessage({ error: lastError.get(m.id + ":" + m.port) || null });
  } else if (m.t === "transfer-cancel") {   // the page's Transfers panel: stop an upload
    const tr = uploads.get(m.id);
    if (tr) { tr.canceled = true; if (tr.s) tr.s.cancel(); }
  } else if (m.t === "forget") {         // the key was forgotten or replaced
    dropChannel(m.id);
    if (reply) reply.postMessage({ ok: true });
  } else if (m.t === "ws") {             // a page's WebSocket (shim.js): its port is the reply port
    if (!reply) return;
    event.waitUntil(openSocket(m, reply, event.source && event.source.id));
  } else if (m.t === "alive") {          // a page with open WebSockets (also keeps this worker running)
    const known = [];
    for (const id of Array.isArray(m.socks) ? m.socks : []) {
      const k = sockets.get(id);
      if (k) { k.seen = Date.now(); known.push(id); }
    }
    if (reply) reply.postMessage({ known });
    event.waitUntil(sweep());
  } else if (m.t === "status") {         // diagnostics (and the viewer test)
    if (reply) reply.postMessage(status());
  }
});

async function viaClient(event, url) {
  const req = event.request;
  let clientUrl = null;
  if (event.clientId) {
    const c = await self.clients.get(event.clientId).catch(() => null);
    if (c) clientUrl = c.url;
  }
  if (!clientUrl && req.referrer && req.referrer !== "about:client") clientUrl = req.referrer;
  const r = V.route(url, B, ORIGIN, clientUrl);
  return r ? handle(event, r) : fetch(req);
}

function textResponse(status, text) {
  return new Response(text + "\n", { status, headers: { "Content-Type": "text/plain; charset=utf-8",
                                                       "Cache-Control": "no-store" } });
}

// ------------------------------------------------------------------ channels
function getChannel(id, rec) {
  let c = channels.get(id);
  if (c && (c.key_id !== rec.key_id || (c.ch && c.ch.closed) ||
            (c.error && Date.now() - c.failedAt > RETRY_AFTER_FAILURE))) {
    dropChannel(id);
    c = null;
  }
  if (c) return c.promise;
  const entry = { key_id: rec.key_id, ch: null, error: null, failedAt: 0 };
  entry.promise = V.openChannel(B, id, rec).then((ch) => {
    entry.ch = ch;
    ch.onclose(() => { if (channels.get(id) === entry) channels.delete(id); });
    return ch;
  }, (e) => {
    entry.error = e;
    entry.failedAt = Date.now();
    throw e;
  });
  entry.promise.catch(() => {});
  channels.set(id, entry);
  return entry.promise;
}

function dropChannel(id, only) {
  const c = channels.get(id);
  if (!c || (only && c.ch !== only)) return;
  channels.delete(id);
  if (c.ch) c.ch.close("replaced");
}

function status() {
  return {
    transport: E.transport,
    channels: [...channels.entries()].map(([id, c]) => ({
      id, transport: c.ch ? c.ch.transport : null, open: !!(c.ch && !c.ch.closed), streams: c.ch ? c.ch.streams.size : 0 })),
    sockets: [...sockets.values()].map((k) => ({ device: k.device, port: k.devPort, open: !!k.open })),
  };
}

// ------------------------------------------------------------------ WebSockets
// The shim's WebSocket posts {t: "ws", sock, id, port, path, protocols} with a MessagePort; this
// worker opens a `ws` stream on the device's channel and relays over the port:
//   worker -> page  {t: "open", protocol} | {t: "msg", text, data: ArrayBuffer, n} | {t: "sent", n}
//                   | {t: "closesent"} | {t: "close", code, reason} (the device's) | {t: "fail", error}
//   page -> worker  {t: "send", text, data: ArrayBuffer, n} | {t: "ack", n} | {t: "close", code, reason} | {t: "drop"}
// Flow control: the stream acks the device as this worker pulls (ack on pull); it pulls the next
// message only while less than WINDOW bytes it posted wait for the page's ack. Pages send
// {t: "alive", socks} to the worker (that also keeps it running); a socket whose page is gone or
// silent for SOCKET_STALE is closed.
const sockets = new Map();           // sock id -> socket state
const SOCKET_STALE = 45000;
const CLOSE_GRACE = 5000;            // after our close: how long the device's answer may take
let lastSweep = 0;

async function openSocket(m, port, clientId) {
  const id = String(m.sock || "");
  const devPort = Number(m.port);
  const path = typeof m.path === "string" ? m.path : "";
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(id) || sockets.has(id) || !V.ID_RE.test(String(m.id)) ||
      !(Number.isInteger(devPort) && devPort >= 1 && devPort <= 65535) || !path.startsWith("/")) {
    try { port.postMessage({ t: "fail", error: "bad WebSocket request" }); } catch (e) { /* gone */ }
    return;
  }
  const k = { id, port, clientId, device: String(m.id), devPort, path, s: null, open: false, done: false,
              unacked: 0, wake: null, seen: Date.now(), sendQ: Promise.resolve(), closeSent: false, timer: null };
  sockets.set(id, k);
  port.onmessage = (e) => fromPage(k, e.data || {});
  try {
    let rec = null;
    try { rec = await V.getKey(k.device); } catch (e) { rec = null; }
    if (!rec) throw new Error("no access key for device " + k.device + " in this browser");
    const jar = await V.getJar(k.device, devPort).catch(() => []);
    const cookie = V.cookieHeader(jar, path, Date.now());
    const headers = [["origin", ORIGIN], ["user-agent", self.navigator.userAgent]];
    if (cookie) headers.push(["cookie", cookie]);
    const protocols = Array.isArray(m.protocols) ? m.protocols.map(String) : [];
    let head = null;
    for (let attempt = 0; !head; attempt++) {
      if (k.done) return;
      const ch = await getChannel(k.device, rec);
      try {
        k.s = ch.openStream({ t: "ws", port: devPort, path, headers, protocols });
        head = await k.s.nextJson();
      } catch (e) {
        if (attempt > 0) throw e;
        dropChannel(k.device, ch);                 // the channel died: once more on a new one
      }
    }
    if (head.t !== "open") throw new Error(String(head.error || "the device refused the WebSocket"));
    if (k.done) { k.s.cancel(); return; }        // the page gave up meanwhile
    k.open = true;
    post(k, { t: "open", protocol: String(head.protocol || "") });
    readLoop(k);
  } catch (e) {
    failSocket(k, V.errorText(e));
  }
}

function post(k, msg, transfer) {
  try { k.port.postMessage(msg, transfer || []); } catch (e) { /* the page is gone */ }
}

function fromPage(k, m) {
  k.seen = Date.now();
  if (m.t === "send") {
    let bytes;
    try { bytes = new Uint8Array(m.data); } catch (e) { return; }
    const n = Number(m.n) || bytes.length;
    k.sendQ = k.sendQ.then(async () => {
      if (!k.open || k.done || k.closeSent) return;
      await k.s.writeMessage(bytes, !!m.text);
      post(k, { t: "sent", n });
    }).catch((e) => failSocket(k, "sending failed: " + V.errorText(e)));
  } else if (m.t === "ack") {
    k.unacked = Math.max(0, k.unacked - (Number(m.n) || 0));
    wake(k);
  } else if (m.t === "close") {
    const code = Number.isInteger(m.code) ? m.code : 1000;
    const reason = String(m.reason || "");
    k.sendQ = k.sendQ.then(async () => {
      if (!k.open || k.done || k.closeSent) return;
      k.closeSent = true;
      await k.s.sendJson({ t: "close", code, reason });
      post(k, { t: "closesent" });
      k.timer = setTimeout(() => { if (k.s && !k.s.done) k.s.cancel(); }, CLOSE_GRACE);
    }).catch(() => failSocket(k, "the connection to the device closed"));
  } else if (m.t === "drop") {
    dropSocket(k);
  }
}

function wake(k) {
  const w = k.wake;
  k.wake = null;
  if (w) w();
}

/** The device's messages to the page, whole (fragments joined); runs until the stream ends. */
async function readLoop(k) {
  const s = k.s;
  let parts = [], size = 0, text = false;
  try {
    for (;;) {
      while (!size && !k.done && k.unacked >= E.WINDOW) await new Promise((r) => { k.wake = r; });
      const ev = await s.next();
      if (ev.data) {
        parts.push(ev.data);
        size += ev.data.length;
        if (ev.flags & E.F_TEXT) text = true;
        if (!(ev.flags & E.F_FIN)) continue;
        const buf = parts.length === 1 ? parts[0] : E.concat(parts, size);
        const n = size, isText = text;
        parts = [];
        size = 0;
        text = false;
        if (k.done) continue;                       // the page is gone: drain until the device's close
        const data = buf.byteOffset === 0 && buf.byteLength === buf.buffer.byteLength ? buf.buffer : buf.slice().buffer;
        k.unacked += n;
        post(k, { t: "msg", text: isText, data, n }, [data]);
        continue;
      }
      const m = ev.json;
      if (m.t === "close") {
        if (!k.done) post(k, { t: "close", code: m.code, reason: String(m.reason || "") });
        endSocket(k);
        return;
      }
      if (m.t === "error") { failSocket(k, String(m.error || "error")); return; }
      if (m.t === "end" || ev.after) { failSocket(k, "the device ended the connection"); return; }
    }
  } catch (e) {
    failSocket(k, V.errorText(e));
  }
}

/** Done with it (the device's close arrived, or the stream failed). */
function endSocket(k) {
  k.done = true;
  clearTimeout(k.timer);
  if (sockets.get(k.id) === k) sockets.delete(k.id);
  k.port.onmessage = null;
  wake(k);
}

/** Not clean: the page gets error + close 1006. */
function failSocket(k, why) {
  if (!k.done) post(k, { t: "fail", error: why });
  endSocket(k);
  if (k.s && !k.s.done) k.s.cancel();
}

/** The page closed it, left, or stopped answering: close the device's side too. */
function dropSocket(k) {
  if (k.done) return;
  endSocket(k);
  const s = k.s;
  if (!s || s.done) return;                       // still connecting: openSocket cancels it
  if (!k.open) return;
  if (!k.closeSent) {
    k.closeSent = true;
    k.sendQ = k.sendQ.then(() => s.sendJson({ t: "close", code: 1001, reason: "" })).catch(() => {});
  }
  setTimeout(() => { if (!s.done) s.cancel(); }, CLOSE_GRACE);
}

/** Close the sockets of pages that are gone or silent. */
async function sweep() {
  const now = Date.now();
  if (now - lastSweep < 10000) return;
  lastSweep = now;
  for (const k of [...sockets.values()]) {
    if (now - k.seen > SOCKET_STALE) { dropSocket(k); continue; }
    if (k.clientId && !(await self.clients.get(k.clientId).catch(() => null))) dropSocket(k);
  }
}
setInterval(() => { sweep().catch(() => {}); }, 15000);

// ------------------------------------------------------------------ requests
async function handle(event, r) {
  const req = event.request;
  const nav = req.mode === "navigate";
  if (r.redirect) return Response.redirect(new URL(r.redirect, ORIGIN).href, 302);
  const where = r.id + ":" + r.port;
  let rec = null;
  try { rec = await V.getKey(r.id); } catch (e) { rec = null; }
  if (!rec) {
    dropChannel(r.id);
    lastError.set(where, "no access key for this device in this browser");
    return nav ? fetch(req) : textResponse(401, "no access key for device " + r.id + " in this browser - " +
                                                "enter it in the device manager");
  }
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  let body = null;
  try { body = hasBody ? await prepareBody(req) : null; } catch (e) { return textResponse(400, "could not read the request body"); }
  const idempotent = req.method === "GET" || req.method === "HEAD" || req.method === "OPTIONS";
  const tr = newUpload(event, r, body);
  for (let attempt = 0; ; attempt++) {
    let ch;
    try {
      ch = await getChannel(r.id, rec);
    } catch (e) {
      lastError.set(where, V.errorText(e));
      report(tr, "failed", V.errorText(e));
      if (nav) return fetch(req);
      return textResponse(e instanceof E.WrongKey ? 403 : 502, V.errorText(e));
    }
    try {
      const res = await request(ch, req, r, body, tr);
      lastError.delete(where);
      if (tr) {
        tr.sent = tr.total;
        report(tr, res.ok ? "done" : "failed", res.ok ? null : "the device answered " + res.status +
                                                         (res.statusText ? " " + res.statusText : ""));
      }
      return res;
    } catch (e) {
      if (!e.device && idempotent && attempt === 0) { dropChannel(r.id, ch); continue; }   // the channel died: once more
      lastError.set(where, V.errorText(e));
      report(tr, tr && tr.canceled ? "canceled" : "failed", tr && tr.canceled ? null : V.errorText(e));
      if (nav) return fetch(req);
      return textResponse(502, V.errorText(e));
    }
  }
}

/** -> {blob, length}. The body as a Blob: its size gives the device an exact Content-Length (many
 *  servers - Arstro Remote's upload too - refuse bodies without one), and Chrome keeps big blobs
 *  (file uploads) on disk instead of in memory. The bytes still go out in CHUNK pieces. */
async function prepareBody(req) {
  let blob = null;
  try { blob = await req.blob(); } catch (e) { blob = null; }
  if (!blob) blob = new Blob([await req.arrayBuffer()]);
  return { blob, length: blob.size };
}

// ------------------------------------------------------------------ uploads for the Transfers panel
// The browser hands a request body to the worker at once, so the page's own upload progress jumps to
// 100 %; the real progress is how fast the worker gets it through the channel. Bodies of at least
// UPLOAD_MIN are reported to the page that sent them (shim.js shows them in its Transfers panel).
const UPLOAD_MIN = 256 * 1024;
const REPORT_EVERY = 250;                 // ms between progress messages
const uploads = new Map();                // id -> tracker
let uploadSeq = 0;

function uploadName(path) {
  try {
    const u = new URL(path, "http://device");
    const n = u.searchParams.get("name") || u.searchParams.get("filename");
    if (n) return n;
    const seg = u.pathname.split("/").filter(Boolean).pop();
    return seg ? decodeURIComponent(seg) : "upload";
  } catch (e) { return "upload"; }
}

function newUpload(event, r, body) {
  if (!body || body.length < UPLOAD_MIN || !event.clientId) return null;
  const tr = { id: "up-" + (++uploadSeq) + "-" + Date.now().toString(36), clientId: event.clientId,
               name: uploadName(r.path), total: body.length, sent: 0, at: 0, s: null, canceled: false };
  uploads.set(tr.id, tr);
  report(tr, "running");
  return tr;
}

function report(tr, state, error) {
  if (!tr) return;
  const now = Date.now();
  if (state === "running" && tr.at && now - tr.at < REPORT_EVERY && tr.sent < tr.total) return;
  tr.at = now;
  if (state !== "running") uploads.delete(tr.id);
  self.clients.get(tr.clientId).then((c) => {
    if (c) c.postMessage({ t: "arstro-transfer", id: tr.id, kind: "upload", name: tr.name, total: tr.total,
                           done: tr.sent, state, error: error || null });
  }).catch(() => {});
}

async function pump(s, body, tr) {
  let reader = null;
  try {
    if (body.length) {
      reader = body.blob.stream().getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
        await s.write(chunk);
        if (tr) { tr.sent += chunk.length; report(tr, "running"); }
      }
      reader = null;
    }
    await s.end();
  } catch (e) {
    if (reader) reader.cancel().catch(() => {});
  }
}

async function request(ch, req, r, body, tr) {
  const headers = V.requestHeaders(req.headers);
  const jar = await V.getJar(r.id, r.port).catch(() => []);
  const cookie = V.cookieHeader(jar, r.path, Date.now());
  if (cookie) headers.push(["cookie", cookie]);
  if (body) headers.push(["content-length", String(body.length)]);
  // browsers send Origin on such requests; the device turns it into http://127.0.0.1:<port>
  if (body && !headers.some(([k]) => k === "origin")) headers.push(["origin", ORIGIN]);
  const s = ch.openStream({ t: "http", method: req.method, port: r.port, path: r.path, headers, body: !!body });
  if (tr) tr.s = s;
  if (body) pump(s, body, tr);
  const head = await s.nextJson();
  if (head.t === "error") {
    const e = new Error(String(head.error || "the device refused the request"));
    e.device = true;
    throw e;
  }
  if (head.t !== "res") {
    const e = new Error("the device answered " + head.t + " instead of a response");
    e.device = true;
    throw e;
  }
  return response(req, r, s, head);
}

function safeReason(reason) {
  const t = String(reason || "");
  return /^[\t\x20-\x7e\x80-\xff]*$/.test(t) ? t : "";
}

async function response(req, r, s, head) {
  const h = V.responseHeaders(head.headers, B, r.id, r.port, ORIGIN);
  if (h.setCookies.length) {
    const now = Date.now();
    await V.updateJar(r.id, r.port, (jar) => V.applySetCookies(jar, h.setCookies, r.path, now)).catch(() => {});
  }
  const status = Number(head.status) || 0;
  if (status < 200 || status > 599) {
    s.cancel();
    const e = new Error("the device answered HTTP status " + status);
    e.device = true;
    throw e;
  }
  const headers = new Headers();
  for (const [k, v] of h.headers) {
    try { headers.append(k, v); } catch (e) { /* a header value the browser does not accept */ }
  }
  const init = { status, statusText: safeReason(head.reason), headers };
  if (req.method === "HEAD" || V.NULL_BODY.has(status)) {
    s.drain();
    return new Response(null, init);
  }
  let body = bodyStream(s);
  if (h.encoding && h.encoding !== "identity") {
    // the device asks for identity; if an app compresses anyway, undo it here when we can
    const fmt = { gzip: "gzip", "x-gzip": "gzip", deflate: "deflate" }[h.encoding];
    if (fmt && typeof DecompressionStream === "function") {
      body = body.pipeThrough(new DecompressionStream(fmt));
      headers.delete("content-encoding");
      headers.delete("content-length");
    } else {
      return new Response(body, init);
    }
  }
  if (V.isHtml(h.contentType) && status !== 206) {
    const bytes = new Uint8Array(await new Response(body).arrayBuffer());
    headers.delete("content-length");
    return new Response(V.injectHtmlBytes(bytes, SHIM), init);
  }
  return new Response(body, init);
}

/** The response body as a stream: one DATA frame per pull; taking it acks it (backpressure). */
function bodyStream(s) {
  return new ReadableStream({
    async pull(ctrl) {
      try {
        for (;;) {
          const ev = await s.next();
          if (ev.data) {
            if (!ev.data.length) continue;
            ctrl.enqueue(ev.data);
            return;
          }
          const t = ev.json.t;
          if (t === "end") { ctrl.close(); return; }
          if (t === "error" || t === "close") {
            ctrl.error(new Error(String(ev.json.error || "the device closed the response")));
            return;
          }
        }
      } catch (e) {
        ctrl.error(e);
      }
    },
    cancel() { s.cancel(); },
  });
}
