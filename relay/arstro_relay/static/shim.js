/* The viewer's page shim (RLY-22): the service worker puts <script src="B/shim.js"></script>
 * right after <head> of every page it proxies, so this runs before the page's own scripts.
 *
 *  - window.WebSocket: same-origin URLs get a class with the browser's API whose instances each
 *    open their own end-to-end channel to the device and a `ws` stream on it (a service worker
 *    cannot proxy WebSockets). Other hosts and the device manager's own sockets use the native one.
 *  - same-origin absolute links, iframes, forms, window.open and history URLs outside B/ are
 *    rewritten into B/d/<id>/<port>/... (navigations outside B/ never reach the service worker).
 *
 * The crypto (e2e.js) and the stores (viewer.js) are loaded on the first WebSocket. The pure
 * helpers are on globalThis.ArstroShim (tested in Node by relay/tests/e2e_vectors.mjs).
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
  const TYPES = ["open", "message", "error", "close"];

  let libP = null;
  function loadScript(file) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = CTX.B + "/" + file;
      s.async = false;
      s.onload = () => { s.remove(); resolve(); };
      s.onerror = () => { s.remove(); reject(new Error("could not load " + file + " from the device manager")); };
      (document.head || document.documentElement).appendChild(s);
    });
  }
  function lib() {
    if (window.ArstroE2E && window.ArstroViewer) return Promise.resolve();
    if (!libP) {
      libP = Promise.all([loadScript("static/e2e.js"), loadScript("static/viewer.js")])
        .catch((e) => { libP = null; throw e; });
    }
    return libP;
  }

  const isArrayBuffer = (x) => Object.prototype.toString.call(x) === "[object ArrayBuffer]";
  const isBlob = (x) => x && typeof x === "object" && typeof x.arrayBuffer === "function" && typeof x.size === "number";
  const originOf = (url) => { try { return new URL(url).origin; } catch (e) { return ""; } };
  const closeCode = (c) => (Number.isInteger(c) && c >= 1000 && c <= 4999 ? c : 1005);

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
                  handlers: {}, ch: null, s: null, sendQ: Promise.resolve(), closeTimer: null };
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
      const s = p.s;
      p.sendQ = p.sendQ.then(async () => {
        const b = bytes || new Uint8Array(await data.arrayBuffer());
        if (p.state === 1) await s.writeMessage(b, text);
      }).catch(() => { if (p.state === 1) fail(this, p, "sending failed"); })
        .then(() => { p.buffered -= size; });
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
      const s = p.s;
      // "close from either side ends it" (PROTOCOL.md 4.3): no answer to wait for; a close the
      // device sends meanwhile is taken instead (readLoop)
      p.sendQ = p.sendQ.then(() => s.sendJson({ t: "close", code: code === undefined ? 1000 : code, reason: reason || "" }))
        .catch(() => {})
        .then(() => { p.closeTimer = setTimeout(() => finish(this, p, p.sentCode, p.sentReason), 50); });
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

  async function start(ws, p, t, protos) {
    try {
      await lib();
      const V = window.ArstroViewer;
      if (p.state !== 0) return;
      const rec = await V.getKey(t.id);
      if (!rec) throw new Error("no access key for device " + t.id + " in this browser");
      const jar = await V.getJar(t.id, t.port).catch(() => []);
      const cookie = V.cookieHeader(jar, t.path, Date.now());
      const headers = [["origin", location.origin], ["user-agent", navigator.userAgent]];
      if (cookie) headers.push(["cookie", cookie]);
      const ch = await V.openChannel(CTX.B, t.id, rec, { WebSocket: NativeWS });
      p.ch = ch;
      if (p.state !== 0) { ch.close("closed"); return; }
      ch.onclose(() => { if (p.state < 3) fail(ws, p, "the channel to the device closed"); });
      const s = ch.openStream({ t: "ws", port: t.port, path: t.path, headers, protocols: protos });
      p.s = s;
      const head = await s.nextJson();
      if (head.t !== "open") throw new Error(String(head.error || "the device refused the WebSocket"));
      if (p.state !== 0) { s.cancel(); ch.close("closed"); return; }
      p.protocol = String(head.protocol || "");
      p.state = 1;
      ws.dispatchEvent(new Event("open"));
      readLoop(ws, p, s);
    } catch (e) {
      fail(ws, p, (e && e.message) || String(e));
    }
  }

  async function readLoop(ws, p, s) {
    const E = window.ArstroE2E;
    let parts = [];
    let size = 0;
    let text = false;
    try {
      for (;;) {
        const ev = await s.next();
        if (ev.data) {
          parts.push(ev.data);
          size += ev.data.length;
          if (ev.flags & E.F_TEXT) text = true;
          if (!(ev.flags & E.F_FIN)) continue;
          const buf = parts.length === 1 ? parts[0] : E.concat(parts, size);
          const isText = text;
          parts = [];
          size = 0;
          text = false;
          if (p.state !== 1) continue;                   // closing: incoming messages are dropped
          let data;
          if (isText) data = dec.decode(buf);
          else if (p.binaryType === "arraybuffer") data = buf.slice().buffer;
          else data = new Blob([buf]);
          ws.dispatchEvent(new MessageEvent("message", { data, origin: originOf(p.url) }));
          continue;
        }
        const m = ev.json;
        if (m.t === "close") {
          let code = closeCode(m.code);
          let reason = String(m.reason || "");
          // the page closed it: the device's answer only confirms; 1005 / 1006 there mean "no code"
          if (p.state === 2 && (code === 1005 || code === 1006)) { code = p.sentCode; reason = p.sentReason; }
          if (code === 1006) fail(ws, p, reason || "the device's local WebSocket closed abnormally");
          else finish(ws, p, code, reason);
          return;
        }
        if (m.t === "error") { fail(ws, p, String(m.error || "error")); return; }
        if (m.t === "end" || ev.after) { fail(ws, p, "the device ended the connection"); return; }
      }
    } catch (e) {
      fail(ws, p, (e && e.message) || String(e));
    }
  }

  function done(p) {
    p.state = 3;
    clearTimeout(p.closeTimer);
    if (p.ch) p.ch.close("closed");
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

  function onPointer(e) {
    const path = typeof e.composedPath === "function" ? e.composedPath() : [e.target];
    for (const n of path) {
      if (n && n.nodeType === 1 && (n.localName === "a" || n.localName === "area")) { fixAnchor(n); return; }
    }
  }
  window.addEventListener("click", onPointer, true);
  window.addEventListener("auxclick", onPointer, true);
  window.addEventListener("contextmenu", onPointer, true);

  // anchors that are clicked without being in the document (downloads made in script)
  const nativeClick = HTMLElement.prototype.click;
  HTMLElement.prototype.click = function () {
    if (this.localName === "a" || this.localName === "area") fixAnchor(this);
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
