/* The browser viewer's shared helpers (RLY-22): routing of the service worker (PROTOCOL.md
 * section 5), Location rewriting, the cookie jar, the HTML shim injection, header mapping and
 * the IndexedDB stores (`arstro-devices`: `keys`, `cookies`).
 *
 * Plain script, no build step: importScripts() in sw.js, a <script> in the pages, and Node
 * (relay/tests/e2e_vectors.mjs tests the pure functions). Everything is on globalThis.ArstroViewer.
 * Needs e2e.js (globalThis.ArstroE2E) only for the key and channel helpers.
 */
(function (root) {
  "use strict";

  const ID_RE = /^[a-z0-9][a-z0-9-]{2,39}$/;
  const DB_NAME = "arstro-devices";
  const DB_VERSION = 1;
  const KEY_TTL = 12 * 3600 * 1000;          // a key that is not remembered: 12 h
  const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

  // ---------------------------------------------------------------- base path and routing
  /** The device manager's base path B from a file served directly under it ("/x/sw.js" -> "/x"). */
  function baseOf(pathname, file) {
    const p = String(pathname);
    const tail = "/" + file;
    return p.endsWith(tail) ? p.slice(0, -tail.length) : p.replace(/\/[^/]*$/, "");
  }

  function inBase(pathname, B) {
    return pathname === B || pathname.startsWith(B + "/");
  }

  /** B/d/<id>/<port>/rest -> {id, port, path: "/rest" ("" when the slash after the port is missing)}. */
  function parseDevicePath(pathname, B) {
    const pre = B + "/d/";
    if (!String(pathname).startsWith(pre)) return null;
    const m = /^([^/]+)\/(\d{1,5})(\/.*)?$/.exec(pathname.slice(pre.length));
    if (!m) return null;
    const port = Number(m[2]);
    if (!ID_RE.test(m[1]) || port < 1 || port > 65535) return null;
    return { id: m[1], port, path: m[3] || "" };
  }

  function devicePrefix(B, id, port) {
    return B + "/d/" + id + "/" + port;
  }

  /** Section 5: which device and port a fetch goes to.
   *  url: URL (or string), clientUrl: the requesting page's URL (the fetch's client, or the
   *  referrer for navigations) or null.
   *  -> {id, port, path, rule: 1|2} | {id, port, redirect} (B/d/<id>/<port> without the slash) | null (network) */
  function route(url, B, origin, clientUrl) {
    const u = typeof url === "string" ? new URL(url) : url;
    if (u.origin !== origin) return null;
    const d = parseDevicePath(u.pathname, B);
    if (d) {
      if (!d.path) return { id: d.id, port: d.port, redirect: u.pathname + "/" + u.search };
      return { id: d.id, port: d.port, path: d.path + u.search, rule: 1 };
    }
    if (inBase(u.pathname, B) || !clientUrl) return null;
    let c;
    try { c = new URL(clientUrl); } catch (e) { return null; }
    if (c.origin !== origin) return null;
    const cd = parseDevicePath(c.pathname, B);
    if (!cd || !cd.path) return null;
    return { id: cd.id, port: cd.port, path: u.pathname + u.search, rule: 2 };
  }

  function defaultPort(protocol) {
    return protocol === "https:" || protocol === "wss:" ? 443 : 80;
  }

  /** A `Location` value from the device -> the value the browser gets. Values pointing at the
   *  device's loopback (http://127.0.0.1:<p>/x, localhost, [::1]) or at absolute paths (/x) go
   *  into B/d/<id>/<port>/...; relative values and other hosts stay as they are. */
  function rewriteLocation(value, B, id, port, origin) {
    const v = String(value == null ? "" : value).trim();
    if (!v) return v;
    const pre = devicePrefix(B, id, port);
    if (v.startsWith("/") && !v.startsWith("//")) {
      const path = v.split(/[?#]/)[0];
      return inBase(path, B) ? v : pre + v;
    }
    let u;
    try { u = new URL(v.startsWith("//") ? "http:" + v : v); } catch (e) { return v; }
    if (!/^(https?|wss?):$/.test(u.protocol)) return v;
    if (LOOPBACK.has(u.hostname)) {
      const p = Number(u.port) || defaultPort(u.protocol);
      return devicePrefix(B, id, p) + (u.pathname || "/") + u.search + u.hash;
    }
    if (origin && u.origin === origin && !inBase(u.pathname, B)) return pre + u.pathname + u.search + u.hash;
    return v;
  }

  // ---------------------------------------------------------------- cookie jar
  // The device's Set-Cookie headers are kept per device and port ("<id>:<port>"); paths are the
  // device's paths (without B/d/<id>/<port>). A cookie: {name, value, path, expires (ms | null =
  // session), secure, httpOnly, sameSite, created}.

  function defaultPath(reqPath) {
    const p = String(reqPath || "/").split("?")[0];
    if (!p.startsWith("/")) return "/";
    const i = p.lastIndexOf("/");
    return i <= 0 ? "/" : p.slice(0, i);
  }

  function pathMatch(reqPath, cookiePath) {
    const p = String(reqPath || "/").split("?")[0] || "/";
    if (p === cookiePath) return true;
    return p.startsWith(cookiePath) && (cookiePath.endsWith("/") || p.charAt(cookiePath.length) === "/");
  }

  /** One Set-Cookie value -> cookie, or null when it is not a cookie. expires 0 = delete it. */
  function parseSetCookie(header, reqPath, now) {
    const parts = String(header).split(";");
    const first = parts.shift();
    const eq = first.indexOf("=");
    if (eq < 0) return null;
    const name = first.slice(0, eq).trim();
    const value = first.slice(eq + 1).trim();           // a quoted value stays quoted, like browsers
    if (!name || /[\s;,=]/.test(name)) return null;
    let path = null, maxAge = null, expires = null, secure = false, httpOnly = false, sameSite = null;
    for (const p of parts) {
      const i = p.indexOf("=");
      const k = (i < 0 ? p : p.slice(0, i)).trim().toLowerCase();
      const v = i < 0 ? "" : p.slice(i + 1).trim();
      if (k === "path") path = v.startsWith("/") ? v : null;
      else if (k === "max-age") { if (/^-?\d+$/.test(v)) maxAge = parseInt(v, 10); }
      else if (k === "expires") { const t = Date.parse(v); if (!isNaN(t)) expires = t; }
      else if (k === "secure") secure = true;
      else if (k === "httponly") httpOnly = true;
      else if (k === "samesite") sameSite = v;
    }
    let exp = null;
    if (maxAge !== null) exp = maxAge <= 0 ? 0 : now + maxAge * 1000;      // Max-Age wins over Expires
    else if (expires !== null) exp = expires <= now ? 0 : expires;
    return { name, value, path: path || defaultPath(reqPath), expires: exp, secure, httpOnly, sameSite };
  }

  function alive(c, now) {
    return c.expires === null || c.expires === undefined || c.expires > now;
  }

  /** jar + the Set-Cookie values of one response (to reqPath) -> the new jar. */
  function applySetCookies(jar, setCookies, reqPath, now) {
    let out = (jar || []).filter((c) => alive(c, now));
    for (const h of setCookies || []) {
      const c = parseSetCookie(h, reqPath, now);
      if (!c) continue;
      const old = out.find((x) => x.name === c.name && x.path === c.path);
      out = out.filter((x) => x !== old);
      if (alive(c, now) && c.expires !== 0) out.push(Object.assign(c, { created: old ? old.created : now }));
    }
    return out;
  }

  /** The Cookie header for a request to reqPath ("" when there is none). */
  function cookieHeader(jar, reqPath, now) {
    const list = (jar || []).filter((c) => alive(c, now) && pathMatch(reqPath, c.path));
    list.sort((a, b) => b.path.length - a.path.length || (a.created || 0) - (b.created || 0));
    return list.map((c) => c.name + "=" + c.value).join("; ");
  }

  // ---------------------------------------------------------------- HTML: the shim
  function shimTag(src) {
    return '<script src="' + String(src).replace(/&/g, "&amp;").replace(/"/g, "&quot;") + '"></script>';
  }

  /** Where the shim goes: right after <head ...>; else after <html ...>; else after the doctype; else 0. */
  function headInsertAt(text) {
    let m = /<head(?:\s[^>]*)?>/i.exec(text);
    if (m) return m.index + m[0].length;
    m = /<html(?:\s[^>]*)?>/i.exec(text);
    if (m) return m.index + m[0].length;
    m = /<!doctype[^>]*>/i.exec(text);
    if (m) return m.index + m[0].length;
    return 0;
  }

  function injectHtml(text, src) {
    const tag = shimTag(src);
    if (text.includes(tag)) return text;
    const i = headInsertAt(text);
    return text.slice(0, i) + tag + text.slice(i);
  }

  /** The same on the raw bytes (any ASCII-compatible charset stays untouched). */
  function injectHtmlBytes(bytes, src) {
    const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    const look = b.subarray(0, Math.min(b.length, 256 * 1024));
    let bin = "";
    for (let i = 0; i < look.length; i += 0x8000) bin += String.fromCharCode.apply(null, look.subarray(i, i + 0x8000));
    const tag = shimTag(src);
    if (bin.includes(tag)) return b;
    const at = headInsertAt(bin);
    const t = new Uint8Array(tag.length);
    for (let i = 0; i < tag.length; i++) t[i] = tag.charCodeAt(i) & 0xff;
    const out = new Uint8Array(b.length + t.length);
    out.set(b.subarray(0, at), 0);
    out.set(t, at);
    out.set(b.subarray(at), at + t.length);
    return out;
  }

  function isHtml(contentType) {
    return /^\s*(text\/html|application\/xhtml\+xml)\b/i.test(contentType || "");
  }

  // ---------------------------------------------------------------- headers
  const HOP = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
               "te", "trailer", "transfer-encoding", "upgrade"];
  // not forwarded from the page: hop-by-hop, the browser's own (the device sets Host; the jar sets Cookie)
  const REQ_DROP = new Set(HOP.concat(["host", "cookie", "cookie2", "content-length", "service-worker",
                                       "service-worker-navigation-preload"]));
  // not given to the browser: hop-by-hop, cookies (into the jar), and Clear-Site-Data (it would
  // wipe the device manager's storage - every key this browser holds)
  const RES_DROP = new Set(HOP.concat(["set-cookie", "set-cookie2", "clear-site-data", "service-worker-allowed"]));

  /** A page request's headers (iterable of [k, v]) -> [[k, v]] for the http stream. */
  function requestHeaders(headers) {
    const out = [];
    for (const [k, v] of headers) {
      const n = String(k).toLowerCase();
      if (!REQ_DROP.has(n) && !n.startsWith("proxy-")) out.push([n, String(v)]);
    }
    return out;
  }

  /** The device's response headers [[k, v]] -> {headers: [[k, v]] for the browser, setCookies,
   *  contentType, encoding, location (rewritten)}. */
  function responseHeaders(list, B, id, port, origin) {
    const out = [];
    const setCookies = [];
    let contentType = "", encoding = "", location = null;
    for (const pair of list || []) {
      if (!Array.isArray(pair) || pair.length < 2) continue;
      const n = String(pair[0]).toLowerCase();
      const v = String(pair[1]);
      if (n === "set-cookie" || n === "set-cookie2") { setCookies.push(v); continue; }
      if (RES_DROP.has(n) || n.startsWith("proxy-")) continue;
      if (n === "content-type") contentType = v;
      if (n === "content-encoding") encoding = v.trim().toLowerCase();
      if (n === "location") { location = rewriteLocation(v, B, id, port, origin); out.push([n, location]); continue; }
      out.push([n, v]);
    }
    return { headers: out, setCookies, contentType, encoding, location };
  }

  const NULL_BODY = new Set([101, 103, 204, 205, 304]);

  // ---------------------------------------------------------------- IndexedDB
  let dbp = null;

  function db() {
    if (dbp) return dbp;
    dbp = new Promise((resolve, reject) => {
      if (!root.indexedDB) { reject(new Error("this browser has no IndexedDB (a private window?)")); return; }
      const r = root.indexedDB.open(DB_NAME, DB_VERSION);
      r.onupgradeneeded = () => {
        const d = r.result;
        if (!d.objectStoreNames.contains("keys")) d.createObjectStore("keys", { keyPath: "id" });
        if (!d.objectStoreNames.contains("cookies")) d.createObjectStore("cookies");
      };
      r.onsuccess = () => {
        const d = r.result;
        d.onversionchange = () => { d.close(); dbp = null; };
        d.onclose = () => { dbp = null; };
        resolve(d);
      };
      r.onerror = () => { dbp = null; reject(r.error || new Error("IndexedDB could not be opened")); };
    });
    return dbp;
  }

  function reqP(r) {
    return new Promise((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
  }

  function doneP(t) {
    return new Promise((resolve, reject) => {
      t.oncomplete = () => resolve();
      t.onerror = t.onabort = () => reject(t.error || new Error("IndexedDB transaction failed"));
    });
  }

  /** The stored key record of a device: {id, key (CryptoKey), key_id, remember, expires} or null
   *  (none, or expired - then it is deleted). */
  async function getKey(id) {
    const d = await db();
    const rec = await reqP(d.transaction("keys").objectStore("keys").get(id));
    if (!rec) return null;
    if (rec.expires && rec.expires <= Date.now()) { await deleteKey(id).catch(() => {}); return null; }
    return rec;
  }

  async function putKey(rec) {
    const d = await db();
    const t = d.transaction("keys", "readwrite");
    t.objectStore("keys").put(rec);
    await doneP(t);
    return rec;
  }

  /** Key text -> stored record. The raw key is turned into a non-extractable CryptoKey and dropped. */
  async function saveKey(id, text, remember) {
    const E = root.ArstroE2E;
    const k = await E.keyFromText(text);
    return putKey({ id, key: k.key, key_id: k.key_id, remember: !!remember,
                    expires: remember ? null : Date.now() + KEY_TTL });
  }

  /** A record for a key that is already a CryptoKey (after a successful check). */
  function keyRecord(id, key, keyIdText, remember) {
    return { id, key, key_id: keyIdText, remember: !!remember, expires: remember ? null : Date.now() + KEY_TTL };
  }

  async function deleteKey(id) {
    const d = await db();
    const t = d.transaction("keys", "readwrite");
    t.objectStore("keys").delete(id);
    await doneP(t);
  }

  async function getJar(id, port) {
    const d = await db();
    return (await reqP(d.transaction("cookies").objectStore("cookies").get(id + ":" + port))) || [];
  }

  /** Read-modify-write of one jar in a single transaction. fn(old list) -> new list. */
  async function updateJar(id, port, fn) {
    const d = await db();
    const t = d.transaction("cookies", "readwrite");
    const s = t.objectStore("cookies");
    const k = id + ":" + port;
    const g = s.get(k);
    g.onsuccess = () => {
      const next = fn(g.result || []);
      if (next && next.length) s.put(next, k);
      else s.delete(k);
    };
    await doneP(t);
  }

  /** Forget every cookie of a device (all ports). */
  async function clearJars(id) {
    const d = await db();
    const t = d.transaction("cookies", "readwrite");
    t.objectStore("cookies").delete(root.IDBKeyRange.bound(id + ":", id + ":￿"));
    await doneP(t);
  }

  // ---------------------------------------------------------------- channels
  function connectUrl(B, id, loc) {
    const l = loc || root.location;
    return (l.protocol === "https:" ? "wss:" : "ws:") + "//" + l.host + B + "/api/connect/" + encodeURIComponent(id);
  }

  /** A channel to device id with a key record (or a CryptoKey / raw bytes). */
  function openChannel(B, id, keyOrRec, opts) {
    const E = root.ArstroE2E;
    const key = keyOrRec && keyOrRec.key ? keyOrRec.key : keyOrRec;
    return E.Channel.open(Object.assign({ url: connectUrl(B, id), key }, opts || {}));
  }

  /** Readable text for an error from a channel / stream. */
  function errorText(e) {
    if (!e) return "failed";
    return String(e.message || e);
  }

  root.ArstroViewer = {
    ID_RE, DB_NAME, KEY_TTL, NULL_BODY,
    baseOf, inBase, parseDevicePath, devicePrefix, route, rewriteLocation,
    defaultPath, pathMatch, parseSetCookie, applySetCookies, cookieHeader,
    shimTag, headInsertAt, injectHtml, injectHtmlBytes, isHtml,
    requestHeaders, responseHeaders,
    db, getKey, putKey, saveKey, keyRecord, deleteKey, getJar, updateJar, clearJars,
    connectUrl, openChannel, errorText,
  };
})(typeof self !== "undefined" ? self : globalThis);
