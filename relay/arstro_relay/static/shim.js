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
 *    browser's blob store as they arrive (big blobs live on disk), shows the progress, then saves
 *    the blob as a normal download under the device's file name.
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

  root.ArstroShim = { shimContext, rewriteUrl, wsTarget, parseDevicePath };

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

  function mb(n) { return (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB"; }

  function notice(text, error) {
    const n = document.createElement("div");
    n.setAttribute("role", error ? "alert" : "status");
    n.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483647;" +
      "max-width:min(90vw,560px);padding:10px 14px;border-radius:8px;color:#fff;" +
      "font:14px/1.4 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.3);background:" + (error ? "#b42318" : "#1f2937");
    n.textContent = text;
    (document.body || document.documentElement).appendChild(n);
    return {
      set(t) { n.textContent = t; },
      end(t, err, ms) {
        n.textContent = t;
        if (err) n.style.background = "#b42318";
        setTimeout(() => n.remove(), ms || 4000);
      },
    };
  }

  async function startDownload(d) {
    let fallback = d.url.pathname.split("/").pop() || "download";
    try { fallback = decodeURIComponent(fallback); } catch (e) { /* keep it */ }
    const box = notice("Downloading " + (d.name || fallback) + " ...");
    try {
      const res = await fetch(d.url.href, { credentials: "same-origin", cache: "no-store" });
      if (!res.ok) throw new Error("the device answered " + res.status + (res.statusText ? " " + res.statusText : ""));
      const name = dispositionName(res.headers.get("content-disposition")) || d.name || fallback;
      const total = Number(res.headers.get("content-length")) || 0;
      const reader = res.body.getReader();
      let blob = new Blob([]), parts = [], pending = 0, got = 0, shown = 0;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        parts.push(value);
        pending += value.length;
        got += value.length;
        if (pending >= FOLD) { blob = new Blob([blob].concat(parts)); parts = []; pending = 0; }
        if (got - shown >= 262144 || (total && got === total)) {
          shown = got;
          box.set("Downloading " + name + " - " + (total ? Math.floor(got * 100 / total) + " % of " + mb(total) : mb(got)));
        }
      }
      if (total && got !== total) throw new Error("only " + got + " of " + total + " bytes arrived");
      blob = new Blob([blob].concat(parts), { type: res.headers.get("content-type") || "application/octet-stream" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      nativeClick.call(a);                       // a blob: link - the browser's own download
      setTimeout(() => URL.revokeObjectURL(url), 120000);
      box.end("Saved " + name + " (" + mb(got) + ")");
    } catch (e) {
      box.end("Download failed: " + ((e && e.message) || e), true, 9000);
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
