/* The viewer's page shim (RLY-22): the service worker puts <script src="B/shim.js"></script>
 * right after <head> of every page it proxies, so this runs before the page's own scripts.
 *
 *  - window.WebSocket: same-origin URLs get a class with the browser's API whose instances are
 *    `ws` streams of the service worker's one channel per device (a service worker cannot proxy
 *    WebSockets, but it can carry them): each socket hands the worker a MessagePort, the worker
 *    opens the stream and relays messages and the close both ways (protocol in sw.js). So all of
 *    a browser's traffic to a device shares one connection or one pipe (PROTOCOL.md section 6).
 *    Other hosts and the device manager's own sockets use the native WebSocket.
 *  - same-origin absolute links, iframes, forms, window.open and history URLs outside B/ are
 *    rewritten into B/d/<id>/<port>/... (navigations outside B/ never reach the service worker).
 *  - downloads (<a download> clicked, also anchors clicked from script without being in the page):
 *    a browser download never reaches the service worker - the browser would save the device
 *    manager's boot page - and Chrome drops a download whose bytes a worker streams. So the shim
 *    fetches the file itself (through the worker and the channel), hands the bytes to the
 *    browser's blob store as they arrive (big blobs live on disk), then saves the blob as a normal
 *    download under the device's file name.
 *  - the Transfers panel (RLY-28): every download above and every upload the service worker
 *    carries (it reports them: the page's own upload progress is meaningless, the browser hands
 *    the body to the worker at once) gets a row - progress, speed, time left, cancel; several at
 *    once form a list. Finished rows go after a minute or with Clear.
 *
 * The pure helpers are on globalThis.ArstroShim (tested in Node by relay/tests/e2e_vectors.mjs).
 */
(function (root) {
  "use strict";

  const ID_RE = /^[a-z0-9][a-z0-9-]{2,39}$/;
  const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

  function inBase(pathname, B) {
    return pathname === B || pathname.startsWith(B + "/");
  }

  function parseDevicePath(pathname, B) {
    const pre = B + "/d/";
    if (!pathname.startsWith(pre)) return null;
    const m = /^([^/]+)\/(\d{1,5})(\/.*)?$/.exec(pathname.slice(pre.length));
    if (!m || !ID_RE.test(m[1]) || Number(m[2]) < 1 || Number(m[2]) > 65535) return null;
    return { id: m[1], port: Number(m[2]), path: m[3] || "" };
  }

  /** The shim's own script URL + the page URL -> {B, id, port, prefix, origin, host} or null
   *  when the page is not a device page. */
  function shimContext(scriptSrc, pageUrl) {
    const s = new URL(scriptSrc, pageUrl);
    const page = new URL(pageUrl);
    const B = s.pathname.replace(/\/shim\.js$/, "");
    const d = parseDevicePath(page.pathname, B);
    if (!d || !d.path || s.origin !== page.origin) return null;
    return { B, id: d.id, port: d.port, prefix: B + "/d/" + d.id + "/" + d.port, origin: page.origin, host: page.host };
  }

  /** A URL the page navigates to / frames / submits -> the URL inside B/d/... or null (leave it). */
  function rewriteUrl(url, ctx, base) {
    let u;
    try { u = new URL(String(url), base); } catch (e) { return null; }
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (u.origin === ctx.origin) {
      if (inBase(u.pathname, ctx.B)) return null;
      return ctx.prefix + u.pathname + u.search + u.hash;
    }
    if (LOOPBACK.has(u.hostname)) {
      const p = Number(u.port) || (u.protocol === "https:" ? 443 : 80);
      return ctx.B + "/d/" + ctx.id + "/" + p + u.pathname + u.search + u.hash;
    }
    return null;
  }

  /** Where a WebSocket URL goes: {id, port, path} through a channel, or null = the native WebSocket. */
  function wsTarget(url, ctx) {
    const u = typeof url === "string" ? new URL(url) : url;
    if (u.host !== ctx.host) return null;
    const d = parseDevicePath(u.pathname, ctx.B);
    if (d && d.path) return { id: d.id, port: d.port, path: d.path + u.search };
    if (inBase(u.pathname, ctx.B)) return null;
    return { id: ctx.id, port: ctx.port, path: u.pathname + u.search };
  }

  // ---- numbers of the Transfers panel
  function fmtBytes(n) {
    n = Math.max(0, Number(n) || 0);
    if (n < 1024) return Math.round(n) + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return (n < 10 ? n.toFixed(1) : String(Math.round(n))) + " " + units[i];
  }

  function fmtRate(bytesPerSecond) { return fmtBytes(bytesPerSecond) + "/s"; }

  /** Seconds -> "42 s", "3 min 05 s", "1 h 02 min"; "" when unknown. */
  function fmtEta(sec) {
    if (!isFinite(sec) || sec < 0) return "";
    sec = Math.ceil(sec);
    const pad = (x) => String(x).padStart(2, "0");
    if (sec < 60) return sec + " s";
    if (sec < 3600) return Math.floor(sec / 60) + " min " + pad(sec % 60) + " s";
    return Math.floor(sec / 3600) + " h " + pad(Math.floor(sec / 60) % 60) + " min";
  }

  /** The smoothed speed after `bytes` arrived in `seconds`: the new rate blended into the old. */
  function nextSpeed(prev, bytes, seconds) {
    if (!(seconds > 0)) return prev || 0;
    const now = bytes / seconds;
    return prev ? prev * 0.7 + now * 0.3 : now;
  }

  root.ArstroShim = { shimContext, rewriteUrl, wsTarget, parseDevicePath, fmtBytes, fmtRate, fmtEta, nextSpeed };

  if (typeof window === "undefined" || typeof document === "undefined" || window.__arstroShim) return;

  const script = document.currentScript ||
    [...document.getElementsByTagName("script")].find((s) => /\/shim\.js(\?|$)/.test(s.src));
  let CTX = null;
  try { CTX = script ? shimContext(script.src, location.href) : null; } catch (e) { CTX = null; }
  if (!CTX) return;
  window.__arstroShim = CTX;

  // ================================================================ the WebSocket
  const NativeWS = window.WebSocket;
  window.__arstroNativeWebSocket = NativeWS;
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const priv = new WeakMap();
  const live = new Map();                    // sock id -> [ws, p] (connecting or open)
  const TYPES = ["open", "message", "error", "close"];
  const ACK_AT = 64 * 1024;                  // ack taken message bytes to the worker at least this often
  const ALIVE_EVERY = 10000;                 // tell the worker which sockets live (keeps it running)
  const ALIVE_TIMEOUT = 30000;               // no answer: the worker is gone, and so are the sockets
  const CLOSE_TIMEOUT = 5000;                // our close: the worker did not even confirm sending it

  const isArrayBuffer = (x) => Object.prototype.toString.call(x) === "[object ArrayBuffer]";
  const isBlob = (x) => x && typeof x === "object" && typeof x.arrayBuffer === "function" && typeof x.size === "number";
  const originOf = (url) => { try { return new URL(url).origin; } catch (e) { return ""; } };
  const closeCode = (c) => (Number.isInteger(c) && c >= 1000 && c <= 4999 ? c : 1005);
  const exact = (b) => (b.byteOffset === 0 && b.byteLength === b.buffer.byteLength ? b.buffer : b.slice().buffer);

  function newId() {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  }

  function worker() {
    return (navigator.serviceWorker && navigator.serviceWorker.controller) || null;
  }

  class WebSocket extends EventTarget {
    constructor(url, protocols) {
      if (arguments.length < 1) throw new TypeError("Failed to construct 'WebSocket': 1 argument required, but only 0 present.");
      let u;
      try { u = new URL(String(url), location.href); } catch (e) {
        throw new DOMException("Failed to construct 'WebSocket': The URL '" + url + "' is invalid.", "SyntaxError");
      }
      if (!/^(wss?|https?):$/.test(u.protocol)) {
        throw new DOMException("Failed to construct 'WebSocket': The URL's scheme must be either 'http', 'https', 'ws', or 'wss'. '" +
                               u.protocol.slice(0, -1) + "' is not allowed.", "SyntaxError");
      }
      if (u.hash) {
        throw new DOMException("Failed to construct 'WebSocket': The URL contains a fragment identifier ('" + u.hash +
                               "'). Fragment identifiers are not allowed in WebSocket URLs.", "SyntaxError");
      }
      const target = wsTarget(u, CTX);
      if (!target) return protocols === undefined ? new NativeWS(url) : new NativeWS(url, protocols);
      super();
      const protos = protocols === undefined ? [] : (Array.isArray(protocols) ? protocols : [protocols]).map(String);
      const seen = new Set();
      for (const p of protos) {
        if (!/^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/.test(p) || seen.has(p)) {
          throw new DOMException("Failed to construct 'WebSocket': The subprotocol '" + p + "' is invalid or duplicated.", "SyntaxError");
        }
        seen.add(p);
      }
      if (u.protocol === "http:") u.protocol = "ws:";
      else if (u.protocol === "https:") u.protocol = "wss:";
      const p = { url: u.href, state: 0, binaryType: "blob", buffered: 0, protocol: "", extensions: "",
                  handlers: {}, sock: newId(), port: null, sendQ: Promise.resolve(), closeTimer: null,
                  ackN: 0, ackTimer: null };
      priv.set(this, p);
      for (const type of TYPES) {
        this.addEventListener(type, (e) => {
          const f = p.handlers[type];
          if (typeof f === "function") f.call(this, e);
        });
      }
      start(this, p, target, protos);
    }

    static [Symbol.hasInstance](x) {
      return priv.has(x) || x instanceof NativeWS;
    }

    get url() { return priv.get(this).url; }
    get readyState() { return priv.get(this).state; }
    get bufferedAmount() { return priv.get(this).buffered; }
    get extensions() { return priv.get(this).extensions; }
    get protocol() { return priv.get(this).protocol; }
    get binaryType() { return priv.get(this).binaryType; }
    set binaryType(v) { if (v === "blob" || v === "arraybuffer") priv.get(this).binaryType = v; }

    send(data) {
      const p = priv.get(this);
      if (p.state === 0) throw new DOMException("Failed to execute 'send' on 'WebSocket': Still in CONNECTING state.", "InvalidStateError");
      let bytes = null;
      let text = false;
      let size;
      if (typeof data === "string") { bytes = enc.encode(data); text = true; size = bytes.length; }
      else if (isArrayBuffer(data)) { bytes = new Uint8Array(data.slice(0)); size = bytes.length; }
      else if (ArrayBuffer.isView(data)) {
        bytes = new Uint8Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
        size = bytes.length;
      } else if (isBlob(data)) { size = data.size; }
      else { bytes = enc.encode(String(data)); text = true; size = bytes.length; }
      p.buffered += size;
      if (p.state !== 1) return;                   // closing / closed: dropped, as the browser does
      // in order (a Blob is read first); the worker answers {t: "sent"} when it went out
      p.sendQ = p.sendQ.then(async () => {
        const b = bytes || new Uint8Array(await data.arrayBuffer());
        if (p.state !== 1) return;
        const ab = exact(b);
        p.port.postMessage({ t: "send", text, data: ab, n: size }, [ab]);
      }).catch(() => { if (p.state === 1) fail(this, p, "sending failed"); });
    }

    close(code, reason) {
      const p = priv.get(this);
      if (code !== undefined) {
        code = Number(code) & 0xffff;
        if (code !== 1000 && (code < 3000 || code > 4999)) {
          throw new DOMException("Failed to execute 'close' on 'WebSocket': The close code must be either 1000, or between 3000 and 4999. " +
                                 code + " is neither.", "InvalidAccessError");
        }
      }
      if (reason !== undefined) {
        reason = String(reason);
        if (enc.encode(reason).length > 123) {
          throw new DOMException("Failed to execute 'close' on 'WebSocket': The close reason must not be greater than 123 UTF-8 bytes.", "SyntaxError");
        }
      }
      if (p.state >= 2) return;
      if (p.state === 0) {
        p.state = 2;
        setTimeout(() => fail(this, p, "closed before the connection was established"), 0);
        return;
      }
      p.state = 2;
      p.sentCode = code === undefined ? 1005 : code;
      p.sentReason = reason || "";
      // "close from either side ends it" (PROTOCOL.md 4.3): no answer to wait for once the worker
      // sent it ({t: "closesent"}); a close the device sends meanwhile is taken instead
      p.closeTimer = setTimeout(() => finish(this, p, p.sentCode, p.sentReason), CLOSE_TIMEOUT);
      p.sendQ = p.sendQ.then(() => {
        if (p.port) p.port.postMessage({ t: "close", code: code === undefined ? 1000 : code, reason: reason || "" });
      }).catch(() => {});
    }
  }

  for (const [i, name] of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].entries()) {
    Object.defineProperty(WebSocket, name, { value: i, enumerable: true });
    Object.defineProperty(WebSocket.prototype, name, { value: i, enumerable: true });
  }
  for (const type of TYPES) {
    Object.defineProperty(WebSocket.prototype, "on" + type, {
      configurable: true, enumerable: true,
      get() { const p = priv.get(this); return (p && p.handlers[type]) || null; },
      set(f) { const p = priv.get(this); if (p) p.handlers[type] = typeof f === "function" ? f : null; },
    });
  }

  function start(ws, p, t, protos) {
    const sw = worker();
    if (!sw) {
      setTimeout(() => fail(ws, p, "the device manager's service worker does not control this page - reload it"), 0);
      return;
    }
    const mc = new MessageChannel();
    p.port = mc.port1;
    p.port.onmessage = (e) => fromWorker(ws, p, e.data || {});
    live.set(p.sock, [ws, p]);
    try {
      sw.postMessage({ t: "ws", sock: p.sock, id: t.id, port: t.port, path: t.path, protocols: protos }, [mc.port2]);
    } catch (e) {
      setTimeout(() => fail(ws, p, "the device manager's service worker cannot be reached"), 0);
      return;
    }
    if (!aliveTimer) aliveTimer = setInterval(aliveCheck, ALIVE_EVERY);
  }

  function fromWorker(ws, p, m) {
    if (p.state === 3) return;
    if (m.t === "open") {
      if (p.state !== 0) return;                     // closed while connecting: the worker was told (drop)
      p.protocol = String(m.protocol || "");
      p.state = 1;
      ws.dispatchEvent(new Event("open"));
    } else if (m.t === "msg") {
      if (p.state === 1) {                           // closing: incoming messages are dropped
        let data;
        if (m.text) data = dec.decode(new Uint8Array(m.data));
        else if (p.binaryType === "arraybuffer") data = m.data;
        else data = new Blob([m.data]);
        ws.dispatchEvent(new MessageEvent("message", { data, origin: originOf(p.url) }));
      }
      ack(p, Number(m.n) || 0);
    } else if (m.t === "sent") {
      p.buffered = Math.max(0, p.buffered - (Number(m.n) || 0));
    } else if (m.t === "closesent") {
      clearTimeout(p.closeTimer);
      p.closeTimer = setTimeout(() => finish(ws, p, p.sentCode, p.sentReason), 50);
    } else if (m.t === "close") {
      let code = closeCode(m.code);
      let reason = String(m.reason || "");
      // the page closed it: the device's answer only confirms; 1005 / 1006 there mean "no code"
      if (p.state === 2 && (code === 1005 || code === 1006)) { code = p.sentCode; reason = p.sentReason; }
      if (code === 1006) fail(ws, p, reason || "the device's local WebSocket closed abnormally");
      else finish(ws, p, code, reason);
    } else if (m.t === "fail") {
      fail(ws, p, String(m.error || "failed"));
    }
  }

  // flow control: the worker sends more only while less than WINDOW bytes wait for these acks
  function ack(p, n) {
    if (!n || !p.port) return;
    p.ackN += n;
    if (p.ackN >= ACK_AT) flushAck(p);
    else if (!p.ackTimer) p.ackTimer = setTimeout(() => flushAck(p), 0);
  }

  function flushAck(p) {
    clearTimeout(p.ackTimer);
    p.ackTimer = null;
    if (p.ackN && p.port) { p.port.postMessage({ t: "ack", n: p.ackN }); p.ackN = 0; }
    if (Date.now() - lastAlive > ALIVE_EVERY / 2) sendAlive(null);   // traffic keeps the worker running too
  }

  // ---- the worker must stay alive while sockets are open (it would be stopped when idle), and a
  // restarted worker does not know the old sockets: the page tells it which ones live and closes
  // those it does not know
  let aliveTimer = null;
  let lastAlive = 0;

  function sendAlive(reply) {
    const sw = worker();
    if (!sw || !live.size) return false;
    lastAlive = Date.now();
    try { sw.postMessage({ t: "alive", socks: [...live.keys()] }, reply ? [reply] : []); } catch (e) { return false; }
    return true;
  }

  function aliveCheck() {
    if (!live.size) { clearInterval(aliveTimer); aliveTimer = null; return; }
    const ids = [...live.keys()];
    const mc = new MessageChannel();
    const lost = (known) => {
      for (const id of ids) {
        const x = live.get(id);
        if (x && !(known && known.has(id))) fail(x[0], x[1], "the device manager's service worker restarted");
      }
    };
    const t = setTimeout(() => { mc.port1.close(); lost(null); }, ALIVE_TIMEOUT);
    mc.port1.onmessage = (e) => {
      clearTimeout(t);
      mc.port1.close();
      lost(new Set((e.data && e.data.known) || []));
    };
    if (!sendAlive(mc.port2)) { clearTimeout(t); lost(null); }
  }

  // leaving the page (or going into the back-forward cache): the sockets end here
  window.addEventListener("pagehide", () => {
    for (const [ws, p] of [...live.values()]) fail(ws, p, null);
  });

  function done(p) {
    p.state = 3;
    clearTimeout(p.closeTimer);
    clearTimeout(p.ackTimer);
    live.delete(p.sock);
    const port = p.port;
    if (port) {
      p.port = null;
      try { port.postMessage({ t: "drop" }); } catch (e) { /* closed */ }
      port.onmessage = null;
      setTimeout(() => port.close(), 1000);
    }
  }

  /** Not clean: error, then close 1006 (as the browser does). */
  function fail(ws, p, why) {
    if (p.state === 3) return;
    done(p);
    if (why) console.warn("WebSocket connection to '" + p.url + "' failed: " + why);
    ws.dispatchEvent(new Event("error"));
    ws.dispatchEvent(new CloseEvent("close", { code: 1006, reason: "", wasClean: false }));
  }

  function finish(ws, p, code, reason) {
    if (p.state === 3) return;
    done(p);
    ws.dispatchEvent(new CloseEvent("close", { code, reason, wasClean: true }));
  }

  Object.defineProperty(WebSocket, "name", { value: "WebSocket" });
  window.WebSocket = WebSocket;

  // ================================================================ links, frames, forms
  const fix = (url) => rewriteUrl(url, CTX, document.baseURI);

  function fixAnchor(a) {
    const attr = a.hasAttribute("href") ? "href" : (a.hasAttribute("xlink:href") ? "xlink:href" : null);
    if (!attr) return;
    const r = fix(a.getAttribute(attr));
    if (r) a.setAttribute(attr, r);
  }

  // ---------------------------------------------------------------- downloads
  const FOLD = 16 * 1024 * 1024;           // bytes collected before they go into the blob store

  /** A download link of this device -> {url, name}, else null (blob:, data:, other sites: the
   *  browser's own download works for those). */
  function downloadOf(a) {
    if (!a.hasAttribute("download") || !a.hasAttribute("href")) return null;
    let u;
    try { u = new URL(a.getAttribute("href"), document.baseURI); } catch (e) { return null; }
    if (u.origin !== CTX.origin || !parseDevicePath(u.pathname, CTX.B)) return null;
    return { url: u, name: a.getAttribute("download") || "" };
  }

  /** The file name of a Content-Disposition header, or null. */
  function dispositionName(cd) {
    const star = /filename\*\s*=\s*[\w-]*'[^']*'([^;]+)/i.exec(cd || "");
    if (star) { try { return decodeURIComponent(star[1].trim()); } catch (e) { /* fall through */ } }
    const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(cd || "");
    return plain ? (plain[1] !== undefined ? plain[1] : plain[2]).trim() || null : null;
  }

  // ---------------------------------------------------------------- the Transfers panel
  // its own scope: the shim has functions of the same names (finish, ...) for its WebSockets
  const Transfers = (() => {
    const rows = new Map();                  // transfer id -> row
    let host = null, list = null, title = null, collapsed = false;
    const SAMPLE = 500;                      // ms between speed samples
    const KEEP_DONE = 60000;                 // a finished row stays this long (failed ones until Clear)
    const BTN = "all:unset;box-sizing:border-box;cursor:pointer;padding:2px 8px;border-radius:6px;" +
                "color:#e5e7eb;background:#374151;font:12px/1.4 system-ui,sans-serif";

    function node(tag, style, text) {
      const e = document.createElement(tag);
      if (style) e.style.cssText = style;    // CSSOM: allowed even where a page's CSP forbids inline styles
      if (text !== undefined) e.textContent = text;
      return e;
    }

    function panel() {
      if (host && host.isConnected) return;
      host = node("div", "all:initial;position:fixed;right:12px;z-index:2147483646");
      host.setAttribute("data-arstro-transfers", "");
      // phones: at the top - apps keep their tab bar at the bottom
      const narrow = window.matchMedia ? window.matchMedia("(max-width: 599px)") : null;
      const place = () => {
        const top = narrow && narrow.matches;
        host.style.top = top ? "8px" : "auto";
        host.style.bottom = top ? "auto" : "12px";
        host.style.right = top ? "8px" : "12px";
      };
      place();
      if (narrow && narrow.addEventListener) narrow.addEventListener("change", place);
      const shadow = host.attachShadow({ mode: "open" });   // the page's CSS cannot reach inside
      const box = node("div", "box-sizing:border-box;width:min(360px,calc(100vw - 24px));max-height:min(60vh,420px);" +
        "display:flex;flex-direction:column;overflow:hidden;border-radius:10px;background:#111827;color:#f9fafb;" +
        "box-shadow:0 8px 28px rgba(0,0,0,.35);font:13px/1.35 system-ui,-apple-system,sans-serif");
      box.setAttribute("role", "region");
      box.setAttribute("aria-label", "Transfers");
      const head = node("div", "display:flex;align-items:center;gap:8px;padding:8px 10px;background:#1f2937;cursor:pointer;user-select:none");
      title = node("div", "flex:1;font-weight:600");
      const clear = node("button", BTN, "Clear");
      clear.title = "Remove the finished transfers";
      clear.onclick = (e) => { e.stopPropagation(); for (const [id, r] of [...rows]) if (r.final) drop(id); };
      const fold = node("button", BTN, "–");
      fold.title = "Fold / unfold";
      const flip = () => { collapsed = !collapsed; list.style.display = collapsed ? "none" : "block"; fold.textContent = collapsed ? "+" : "–"; };
      fold.onclick = (e) => { e.stopPropagation(); flip(); };
      head.onclick = flip;
      list = node("div", "overflow:auto;display:" + (collapsed ? "none" : "block"));
      head.append(title, clear, fold);
      box.append(head, list);
      shadow.append(box);
      (document.body || document.documentElement).appendChild(host);
    }

    function heading() {
      if (!rows.size) { if (host) host.remove(); host = null; return; }
      const running = [...rows.values()].filter((r) => !r.final).length;
      title.textContent = "Transfers · " + (running ? running + " running" : "all finished");
    }

    /** A new row: kind "download" | "upload"; cancel() stops it (or null). */
    function track(id, kind, name, total, cancel) {
      panel();
      const now = Date.now();
      const r = { id, kind, name, total: total || 0, done: 0, speed: 0, t0: now, at: now, atDone: 0, final: false, cancel };
      r.el = node("div", "padding:7px 10px;border-top:1px solid #374151");
      r.el.setAttribute("data-transfer", kind);
      r.el.setAttribute("data-state", "running");
      const top = node("div", "display:flex;align-items:center;gap:6px");
      const arrow = node("span", "flex:none;width:14px;text-align:center;font-weight:700;color:" +
                         (kind === "upload" ? "#93c5fd" : "#86efac"), kind === "upload" ? "↑" : "↓");
      arrow.title = kind;
      const label = r.label = node("span", "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap", name);
      label.title = name;
      r.stop = node("button", BTN, "✕");
      r.stop.title = "Cancel";
      r.stop.onclick = () => { if (!r.final && r.cancel) r.cancel(); };
      if (!cancel) r.stop.style.display = "none";
      top.append(arrow, label, r.stop);
      const bar = node("div", "height:4px;margin:6px 0 4px;border-radius:2px;background:#374151;overflow:hidden");
      r.fill = node("div", "height:100%;width:0;background:#60a5fa;transition:width .25s");
      bar.append(r.fill);
      r.info = node("div", "font-size:12px;color:#9ca3af;font-variant-numeric:tabular-nums");
      r.el.append(top, bar, r.info);
      list.prepend(r.el);
      rows.set(id, r);
      paint(r);
      heading();
      return r;
    }

    function progress(id, done, total) {
      const r = rows.get(id);
      if (!r || r.final) return;
      if (total) r.total = total;
      r.done = done;
      const now = Date.now();
      if (now - r.at >= SAMPLE) {
        r.speed = nextSpeed(r.speed, done - r.atDone, (now - r.at) / 1000);
        r.at = now;
        r.atDone = done;
      }
      paint(r);
    }

    function paint(r) {
      if (r.final) return;
      if (r.total) r.fill.style.width = Math.min(100, r.done * 100 / r.total).toFixed(1) + "%";
      else { r.fill.style.width = "100%"; r.fill.style.opacity = ".35"; }
      const parts = [fmtBytes(r.done) + (r.total ? " of " + fmtBytes(r.total) : "")];
      parts.push(r.speed ? fmtRate(r.speed) : "starting…");
      if (r.total && r.speed) parts.push(fmtEta((r.total - r.done) / r.speed) + " left");
      r.info.textContent = parts.join(" · ");
    }

    /** state: "done" | "failed" | "canceled". */
    function finish(id, state, error) {
      const r = rows.get(id);
      if (!r || r.final) return;
      r.final = true;
      r.el.setAttribute("data-state", state);
      r.stop.style.display = "none";
      const secs = Math.max(0.001, (Date.now() - r.t0) / 1000);
      r.fill.style.opacity = "1";
      if (state === "done") {
        r.fill.style.width = "100%";
        r.fill.style.background = "#22c55e";
        r.info.textContent = (r.kind === "upload" ? "Uploaded " : "Saved ") + fmtBytes(r.done) + " in " + fmtEta(secs) +
                             " · " + fmtRate(r.done / secs);
        setTimeout(() => drop(id), KEEP_DONE);
      } else {
        r.fill.style.background = state === "failed" ? "#ef4444" : "#6b7280";
        r.info.textContent = state === "failed" ? "Failed: " + (error || "unknown error") : "Canceled";
        r.info.style.color = state === "failed" ? "#fca5a5" : "#9ca3af";
      }
      heading();
    }

    function drop(id) {
      const r = rows.get(id);
      if (!r) return;
      rows.delete(id);
      r.el.remove();
      heading();
    }

    function rename(id, name) {
      const r = rows.get(id);
      if (!r || r.name === name) return;
      r.name = name;
      r.label.textContent = name;
      r.label.title = name;
    }

    return { track, progress, finish, rename, has: (id) => rows.has(id) };

  })();

  // uploads: the service worker reports what it sends (sw.js, "arstro-transfer")
  if (navigator.serviceWorker) {
    navigator.serviceWorker.addEventListener("message", (e) => {
      const m = e.data || {};
      if (m.t !== "arstro-transfer" || typeof m.id !== "string") return;
      if (!Transfers.has(m.id)) {
        Transfers.track(m.id, m.kind === "download" ? "download" : "upload", String(m.name || "upload"), m.total, () => {
          const c = navigator.serviceWorker.controller;
          if (c) c.postMessage({ t: "transfer-cancel", id: m.id });
        });
      }
      Transfers.progress(m.id, Number(m.done) || 0, Number(m.total) || 0);
      if (m.state && m.state !== "running") Transfers.finish(m.id, m.state, m.error);
    });
    if (navigator.serviceWorker.startMessages) navigator.serviceWorker.startMessages();
  }

  let downloadSeq = 0;
  async function startDownload(d) {
    let fallback = d.url.pathname.split("/").pop() || "download";
    try { fallback = decodeURIComponent(fallback); } catch (e) { /* keep it */ }
    const abort = new AbortController();
    const id = "down-" + (++downloadSeq) + "-" + Date.now().toString(36);
    Transfers.track(id, "download", d.name || fallback, 0, () => abort.abort());
    try {
      const res = await fetch(d.url.href, { credentials: "same-origin", cache: "no-store", signal: abort.signal });
      if (!res.ok) throw new Error("the device answered " + res.status + (res.statusText ? " " + res.statusText : ""));
      const name = dispositionName(res.headers.get("content-disposition")) || d.name || fallback;
      Transfers.rename(id, name);
      const total = Number(res.headers.get("content-length")) || 0;
      const reader = res.body.getReader();
      let blob = new Blob([]), parts = [], pending = 0, got = 0;
      Transfers.progress(id, 0, total);
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        parts.push(value);
        pending += value.length;
        got += value.length;
        if (pending >= FOLD) { blob = new Blob([blob].concat(parts)); parts = []; pending = 0; }
        Transfers.progress(id, got, total);
      }
      if (total && got !== total) throw new Error("only " + fmtBytes(got) + " of " + fmtBytes(total) + " arrived");
      blob = new Blob([blob].concat(parts), { type: res.headers.get("content-type") || "application/octet-stream" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      nativeClick.call(a);                       // a blob: link - the browser's own download
      setTimeout(() => URL.revokeObjectURL(url), 120000);
      Transfers.finish(id, "done");
    } catch (e) {
      if (abort.signal.aborted) Transfers.finish(id, "canceled");
      else Transfers.finish(id, "failed", (e && e.message) || String(e));
    }
  }

  function onPointer(e) {
    const path = typeof e.composedPath === "function" ? e.composedPath() : [e.target];
    for (const n of path) {
      if (n && n.nodeType === 1 && (n.localName === "a" || n.localName === "area")) {
        fixAnchor(n);
        if (e.type === "click" && !e.defaultPrevented) {
          const d = downloadOf(n);
          if (d) { e.preventDefault(); startDownload(d); }
        }
        return;
      }
    }
  }
  window.addEventListener("click", onPointer, true);
  window.addEventListener("auxclick", onPointer, true);
  window.addEventListener("contextmenu", onPointer, true);

  // anchors that are clicked without being in the document (downloads made in script)
  const nativeClick = HTMLElement.prototype.click;
  HTMLElement.prototype.click = function () {
    if (this.localName === "a" || this.localName === "area") {
      fixAnchor(this);
      if (!this.isConnected) {                       // its click never reaches the window listener
        const d = downloadOf(this);
        if (d) { startDownload(d); return; }
      }
    }
    return nativeClick.apply(this, arguments);
  };

  function fixForm(form, submitter) {
    if (submitter && submitter.hasAttribute && submitter.hasAttribute("formaction")) {
      const r = fix(submitter.getAttribute("formaction"));
      if (r) submitter.setAttribute("formaction", r);
    }
    const r = fix(form.getAttribute("action") === null ? location.href : form.action);
    if (r) form.setAttribute("action", r);
  }
  window.addEventListener("submit", (e) => { if (e.target && e.target.localName === "form") fixForm(e.target, e.submitter); }, true);
  const nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    fixForm(this, null);
    return nativeSubmit.apply(this, arguments);
  };

  const FRAMES = new Set(["iframe", "frame"]);
  const srcDesc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, "src");
  if (srcDesc && srcDesc.set) {
    Object.defineProperty(HTMLIFrameElement.prototype, "src", {
      configurable: true, enumerable: true, get: srcDesc.get,
      set(v) { srcDesc.set.call(this, fix(v) || v); },
    });
  }
  const nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (FRAMES.has(this.localName) && String(name).toLowerCase() === "src") {
      const r = fix(value);
      if (r) value = r;
    }
    return nativeSetAttribute.call(this, name, value);
  };
  function fixFrame(el) {
    const v = el.getAttribute("src");
    if (v === null) return;
    const r = fix(v);
    if (r) nativeSetAttribute.call(el, "src", r);
  }
  new MutationObserver((records) => {
    for (const rec of records) {
      if (rec.type === "attributes") {
        if (FRAMES.has(rec.target.localName)) fixFrame(rec.target);
        continue;
      }
      for (const n of rec.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (FRAMES.has(n.localName)) fixFrame(n);
        if (n.querySelectorAll) for (const f of n.querySelectorAll("iframe[src], frame[src]")) fixFrame(f);
      }
    }
  }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });

  const nativeOpen = window.open;
  window.open = function (url) {
    const args = Array.prototype.slice.call(arguments);
    if (url !== undefined && url !== null && url !== "") {
      const r = fix(url);
      if (r) args[0] = r;
    }
    return nativeOpen.apply(window, args);
  };

  for (const m of ["pushState", "replaceState"]) {
    const native = History.prototype[m];
    History.prototype[m] = function (state, title, url) {
      if (url !== undefined && url !== null) {
        const r = fix(url);
        if (r) return native.call(this, state, title, r);
      }
      return native.apply(this, arguments);
    };
  }

  // location.href = "/x" and friends cannot be patched; the Navigation API (Chrome) sees them
  if (window.navigation && typeof window.navigation.addEventListener === "function") {
    window.navigation.addEventListener("navigate", (e) => {
      try {
        if (!e.cancelable || e.hashChange || e.downloadRequest !== null || e.formData ||
            e.navigationType === "traverse" || (e.destination && e.destination.sameDocument)) return;
        const r = fix(e.destination.url);
        if (!r) return;
        e.preventDefault();
        if (e.navigationType === "replace") location.replace(r);
        else location.assign(r);
      } catch (x) { /* leave the navigation alone */ }
    });
  }

  // an app's own service worker would take this page away from the viewer's
  if (navigator.serviceWorker && navigator.serviceWorker.register) {
    try {
      Object.defineProperty(navigator.serviceWorker, "register", {
        configurable: true,
        value: () => Promise.reject(new DOMException("apps cannot register their own service worker through the device manager",
                                                      "SecurityError")),
      });
    } catch (e) { /* leave it */ }
  }
})(typeof window !== "undefined" ? window : globalThis);
