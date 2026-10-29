/* The browser viewer's service worker (RLY-22). Registered as B/sw.js with scope B/ (the relay
 * sends Service-Worker-Allowed: B/). It turns the pages of B/d/<id>/<port>/... into HTTP
 * requests to 127.0.0.1:<port> on device <id>, through the end-to-end encrypted channel.
 *
 * Routing (docs/relay/PROTOCOL.md section 5), for every fetch:
 *   1. path under B/d/<id>/<port>/  -> device <id>, port, path = the rest + query
 *   2. a same-origin URL outside B/ whose requesting page (the fetch's client, or the referrer
 *      for navigations) is under B/d/<id>/<port>/  -> that device and port, path = path + query
 *   3. else the network
 * One channel per device while the worker lives (reopened when it fails). Request bodies go
 * out in CHUNK pieces (with their Content-Length), responses come back as streams with
 * backpressure (ack on pull).
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
  } else if (m.t === "forget") {         // the key was forgotten or replaced
    dropChannel(m.id);
    if (reply) reply.postMessage({ ok: true });
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
  for (let attempt = 0; ; attempt++) {
    let ch;
    try {
      ch = await getChannel(r.id, rec);
    } catch (e) {
      lastError.set(where, V.errorText(e));
      if (nav) return fetch(req);
      return textResponse(e instanceof E.WrongKey ? 403 : 502, V.errorText(e));
    }
    try {
      const res = await request(ch, req, r, body);
      lastError.delete(where);
      return res;
    } catch (e) {
      if (!e.device && idempotent && attempt === 0) { dropChannel(r.id, ch); continue; }   // the channel died: once more
      lastError.set(where, V.errorText(e));
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

async function pump(s, body) {
  let reader = null;
  try {
    if (body.length) {
      reader = body.blob.stream().getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        await s.write(value instanceof Uint8Array ? value : new Uint8Array(value));
      }
      reader = null;
    }
    await s.end();
  } catch (e) {
    if (reader) reader.cancel().catch(() => {});
  }
}

async function request(ch, req, r, body) {
  const headers = V.requestHeaders(req.headers);
  const jar = await V.getJar(r.id, r.port).catch(() => []);
  const cookie = V.cookieHeader(jar, r.path, Date.now());
  if (cookie) headers.push(["cookie", cookie]);
  if (body) headers.push(["content-length", String(body.length)]);
  // browsers send Origin on such requests; the device turns it into http://127.0.0.1:<port>
  if (body && !headers.some(([k]) => k === "origin")) headers.push(["origin", ORIGIN]);
  const s = ch.openStream({ t: "http", method: req.method, port: r.port, path: r.path, headers, body: !!body });
  if (body) pump(s, body);
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
