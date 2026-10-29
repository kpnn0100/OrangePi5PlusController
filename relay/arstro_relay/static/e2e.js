/* The end-to-end channel between a viewer (this browser) and a machine, through the device
 * manager (RLY-03). The browser twin of server/arstro_remote/relay/e2e.py - same constants,
 * same crypto, same framing - and tested against the same docs/relay/vectors.json
 * (relay/tests/e2e_vectors.mjs). Normative description: docs/relay/PROTOCOL.md section 4.
 *
 * Plain script, no build step: loads with importScripts() in the service worker, as a page
 * <script>, and in Node 22 (global crypto.subtle). Everything is on globalThis.ArstroE2E.
 *
 *   viewer                                   device
 *   hello  {v, cn}                  ->
 *                                   <-       hello {v, sn, proof(device)}   (or error)
 *   (checks the device's proof first: a wrong key or a fake device stops here)
 *   proof  {proof(viewer)}          ->
 *                                   <-       [encrypted] ready {...}        (or error "wrong key")
 *   [encrypted frames both ways]
 *
 * WebCrypto is asynchronous, so ordering is explicit: a frame's nonce is taken when seal() /
 * open() is called, sending goes through one promise chain per channel and received messages
 * are handled one after the other - the counters can never get out of step.
 */
(function (root) {
  "use strict";

  const VERSION = 1;
  const LABEL = "arstro-relay/1";
  const KEY_PREFIX = "ak1_";
  const KEY_LEN = 32;
  const NONCE_LEN = 32;            // cn / sn
  const CHUNK = 64 * 1024;         // largest DATA payload in one frame
  const WINDOW = 512 * 1024;       // unacknowledged DATA bytes per stream and direction
  const MAX_MESSAGE = 1 << 20;     // largest WebSocket message anywhere on the path
  const K_JSON = 1;
  const K_DATA = 2;
  const F_FIN = 1;
  const F_TEXT = 2;
  const HEADER = 6;                // kind u8 | stream u32 BE | flags u8
  const HANDSHAKE_TIMEOUT = 15000;
  const PING_EVERY = 20000;
  const DEAD_AFTER = 65000;        // nothing from the device for this long: the channel is gone

  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const strictDec = new TextDecoder("utf-8", { fatal: true });

  function subtle() {
    const c = root.crypto;
    if (!c || !c.subtle) throw new Error("WebCrypto is not available: open the device manager over https (or http://localhost)");
    return c.subtle;
  }

  class ProtocolError extends Error {
    constructor(msg) { super(msg); this.name = "ProtocolError"; }
  }

  class WrongKey extends Error {
    constructor(msg) { super(msg || "wrong access key for this device"); this.name = "WrongKey"; }
  }

  /** Refused by the relay or the device (offline, busy, port not allowed, ...). */
  class Refused extends Error {
    constructor(msg) { super(msg); this.name = "Refused"; }
  }

  // ---------------------------------------------------------------- encoding helpers
  function u8(data) {
    if (data instanceof Uint8Array) return data;
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    if (Array.isArray(data)) return Uint8Array.from(data);
    throw new TypeError("bytes expected");
  }

  function concat(parts, total) {
    if (total === undefined) total = parts.reduce((n, p) => n + p.length, 0);
    const out = new Uint8Array(total);
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }

  function b64e(data) {
    const b = u8(data);
    let s = "";
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function b64d(text) {
    let t = String(text == null ? "" : text).replace(/=+$/, "");
    if (!/^[A-Za-z0-9_\-+/]*$/.test(t) || t.length % 4 === 1) throw new ProtocolError("bad base64");
    t = t.replace(/-/g, "+").replace(/_/g, "/");
    let bin;
    try { bin = atob(t + "===".slice((t.length + 3) % 4)); } catch (e) { throw new ProtocolError("bad base64"); }
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function equalBytes(a, b) {
    a = u8(a); b = u8(b);
    if (a.length !== b.length) return false;
    let d = 0;
    for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
    return d === 0;
  }

  // ---------------------------------------------------------------- the access key
  /** Key text -> 32 bytes. Spaces and line breaks from copy-paste are ignored. */
  function parseKey(text) {
    const t = String(text == null ? "" : text).replace(/\s+/g, "");
    if (!t.startsWith(KEY_PREFIX)) throw new Error("an access key starts with " + KEY_PREFIX);
    let raw;
    try { raw = b64d(t.slice(KEY_PREFIX.length)); } catch (e) {
      throw new Error("the access key has characters that do not belong in it");
    }
    if (raw.length !== KEY_LEN) throw new Error("the access key is too short or too long - copy all of it");
    return raw;
  }

  /** True when the text looks like a whole access key (for enabling buttons). */
  function isKey(text) {
    try { parseKey(text); return true; } catch (e) { return false; }
  }

  /** A short public fingerprint of a key, to tell keys apart in a UI. Not secret, not the key. */
  async function keyId(raw) {
    const data = concat([enc.encode(LABEL + " key-id "), u8(raw)]);
    const h = new Uint8Array(await subtle().digest("SHA-256", data));
    return b64e(h.subarray(0, 6));
  }

  /** Raw key bytes -> a NON-extractable HKDF CryptoKey (what the browser stores). */
  function importKey(raw) {
    return subtle().importKey("raw", u8(raw), "HKDF", false, ["deriveBits"]);
  }

  /** Key text -> {key: CryptoKey (non-extractable), key_id}. The raw bytes are dropped. */
  async function keyFromText(text) {
    const raw = parseKey(text);
    try {
      return { key: await importKey(raw), key_id: await keyId(raw) };
    } finally {
      raw.fill(0);
    }
  }

  // ---------------------------------------------------------------- crypto
  /** -> 96 bytes: viewer->device key | device->viewer key | proof key. key: CryptoKey or bytes. */
  async function deriveBits(key, cn, sn) {
    cn = u8(cn); sn = u8(sn);
    if (cn.length !== NONCE_LEN || sn.length !== NONCE_LEN) throw new ProtocolError("cn and sn must be " + NONCE_LEN + " bytes");
    const k = (root.CryptoKey && key instanceof root.CryptoKey) || (key && key.type === "secret" && key.algorithm) ? key : await importKey(key);
    const bits = await subtle().deriveBits({ name: "HKDF", hash: "SHA-256", salt: concat([cn, sn]), info: enc.encode(LABEL) }, k, 96 * 8);
    return new Uint8Array(bits);
  }

  /** -> {v2d, d2v, pk} as raw bytes (tests and diagnostics). */
  async function derive(key, cn, sn) {
    const okm = await deriveBits(key, cn, sn);
    return { v2d: okm.slice(0, 32), d2v: okm.slice(32, 64), pk: okm.slice(64, 96) };
  }

  function aesKey(raw) {
    return subtle().importKey("raw", u8(raw), { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
  }

  function hmacKey(raw) {
    return subtle().importKey("raw", u8(raw), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
  }

  /** HMAC-SHA256(proof key, LABEL + " " + role), role "device" or "viewer". pk: bytes or HMAC CryptoKey. */
  async function proof(pk, role) {
    const k = pk instanceof Uint8Array || ArrayBuffer.isView(pk) || pk instanceof ArrayBuffer ? await hmacKey(pk) : pk;
    return new Uint8Array(await subtle().sign("HMAC", k, enc.encode(LABEL + " " + role)));
  }

  function nonce(counter) {
    const n = new Uint8Array(12);
    const dv = new DataView(n.buffer);
    dv.setUint32(4, Math.floor(counter / 0x100000000));
    dv.setUint32(8, counter >>> 0);
    return n;
  }

  /** One side of an established channel: seal outgoing frames, open incoming ones.
   *  The nonce is taken synchronously when seal() / open() is called. */
  class Secure {
    constructor(sendKey, recvKey) {
      this._send = sendKey;
      this._recv = recvKey;
      this._sn = 0;
      this._rn = 0;
    }

    /** From raw 32-byte keys (tests). */
    static async fromRaw(sendRaw, recvRaw) {
      return new Secure(await aesKey(sendRaw), await aesKey(recvRaw));
    }

    seal(kind, stream, payload, flags) {
      const p = payload ? u8(payload) : new Uint8Array(0);
      const pt = new Uint8Array(HEADER + p.length);
      const dv = new DataView(pt.buffer);
      dv.setUint8(0, kind);
      dv.setUint32(1, stream >>> 0);
      dv.setUint8(5, flags || 0);
      pt.set(p, HEADER);
      const iv = nonce(this._sn++);
      return subtle().encrypt({ name: "AES-GCM", iv }, this._send, pt).then((ct) => new Uint8Array(ct));
    }

    sealJson(stream, obj) {
      return this.seal(K_JSON, stream, enc.encode(JSON.stringify(obj)), 0);
    }

    /** -> Promise<{kind, stream, flags, payload}>; rejects with ProtocolError if it does not decrypt. */
    open(ciphertext) {
      const iv = nonce(this._rn++);
      let ct;
      try { ct = u8(ciphertext); } catch (e) { return Promise.reject(new ProtocolError("binary frame expected")); }
      return subtle().decrypt({ name: "AES-GCM", iv }, this._recv, ct).then((buf) => {
        const pt = new Uint8Array(buf);
        if (pt.length < HEADER) throw new ProtocolError("frame too short");
        const dv = new DataView(pt.buffer, pt.byteOffset, pt.byteLength);
        return { kind: dv.getUint8(0), stream: dv.getUint32(1), flags: dv.getUint8(5), payload: pt.subarray(HEADER) };
      }, () => {
        throw new ProtocolError("a frame did not decrypt (wrong key, reordered or changed on the way)");
      });
    }
  }

  function decodeJson(payload) {
    let msg;
    try { msg = JSON.parse(strictDec.decode(u8(payload))); } catch (e) { throw new ProtocolError("bad JSON frame: " + e.message); }
    if (!msg || typeof msg !== "object" || Array.isArray(msg) || typeof msg.t !== "string") {
      throw new ProtocolError("a JSON frame must be an object with t");
    }
    return msg;
  }

  // ---------------------------------------------------------------- handshake
  function randomBytes(n) {
    const b = new Uint8Array(n);
    root.crypto.getRandomValues(b);
    return b;
  }

  /** -> {cn, msg}. The viewer sends msg as a text WebSocket message. */
  function viewerHello(cn) {
    cn = cn ? u8(cn) : randomBytes(NONCE_LEN);
    return { cn, msg: { t: "hello", v: VERSION, cn: b64e(cn) } };
  }

  /** Viewer side, on the device's hello -> {secure, msg (the proof to send)}. Throws WrongKey when
   *  the device does not prove the key - nothing derived from the key has been sent then. */
  async function viewerCheck(key, cn, answer) {
    if (!answer || answer.t === "error") throw new Refused(String((answer && answer.error) || "refused"));
    if (answer.t !== "hello" || answer.v !== VERSION) throw new ProtocolError("expected hello v" + VERSION);
    const okm = await deriveBits(key, cn, b64d(answer.sn));
    const pk = await hmacKey(okm.subarray(64, 96));
    let theirs;
    try { theirs = b64d(answer.proof); } catch (e) { throw new WrongKey(); }
    const ok = theirs.length === 32 &&
      await subtle().verify("HMAC", pk, theirs, enc.encode(LABEL + " device"));
    if (!ok) { okm.fill(0); throw new WrongKey(); }
    const mine = await proof(pk, "viewer");
    const secure = new Secure(await aesKey(okm.subarray(0, 32)), await aesKey(okm.subarray(32, 64)));
    okm.fill(0);
    return { secure, msg: { t: "proof", proof: b64e(mine) } };
  }

  /** Device side (tests, fake devices): on the viewer's hello -> {state, msg}. */
  async function deviceAnswer(key, hello, sn) {
    if (!hello || hello.t !== "hello" || hello.v !== VERSION) throw new ProtocolError("expected hello v" + VERSION);
    const cn = b64d(hello.cn);
    sn = sn ? u8(sn) : randomBytes(NONCE_LEN);
    const d = await derive(key, cn, sn);
    return { state: d, msg: { t: "hello", v: VERSION, sn: b64e(sn), proof: b64e(await proof(d.pk, "device")) } };
  }

  /** Device side (tests): on the viewer's proof -> Secure. Throws WrongKey. */
  async function deviceCheck(state, msg) {
    if (!msg || msg.t !== "proof") throw new ProtocolError("expected proof");
    if (!equalBytes(await proof(state.pk, "viewer"), b64d(msg.proof))) throw new WrongKey("the viewer has a different access key");
    return Secure.fromRaw(state.d2v, state.v2d);
  }

  // ---------------------------------------------------------------- streams
  const TERMINAL = new Set(["close", "error", "ports"]);

  /** One stream of a channel (stream id >= 1). Incoming events are queued and handed out by
   *  next() in order: {json} or {data, flags}. Handing DATA out counts it as consumed and acks
   *  it (flow control: "ack on pull"); writes wait while WINDOW bytes are unacknowledged. */
  class Stream {
    constructor(channel, id, localEnded) {
      this.channel = channel;
      this.id = id;
      this.done = false;            // nothing more either way: removed from the channel
      this.remoteEnded = false;     // the device sent end (its direction is finished)
      this.localEnded = !!localEnded;
      this.error = null;
      this._queue = [];
      this._waiter = null;
      this._outUnacked = 0;
      this._credit = [];
      this._inUnacked = 0;
    }

    // ---- incoming (called by the channel, in frame order)
    _json(msg) {
      if (msg.t === "ack") {
        this._outUnacked = Math.max(0, this._outUnacked - (Number(msg.n) || 0));
        this._wake();
        return;
      }
      if (msg.t === "end") {
        this.remoteEnded = true;
        if (this.localEnded) this._finished();
      } else if (TERMINAL.has(msg.t)) {
        this.remoteEnded = true;
        this._finished();
      }
      this._push({ json: msg });
    }

    _data(payload, flags) {
      if (this.remoteEnded) return;
      this._push({ data: payload, flags });
    }

    _push(ev) {
      if (this._waiter) {
        const w = this._waiter;
        this._waiter = null;
        this._hand(ev, w.resolve);
      } else {
        this._queue.push(ev);
      }
    }

    _hand(ev, resolve) {
      if (ev.data) this.consumed(ev.data.length);
      resolve(ev);
    }

    _finished() {
      if (this.done) return;
      this.done = true;
      this.channel._forget(this.id);
      this._wake();
    }

    _fail(err) {
      this.error = this.error || err;
      this._finished();
      if (this._waiter) {
        const w = this._waiter;
        this._waiter = null;
        w.reject(this.error);
      }
    }

    /** The next event: {json: {...}} or {data: Uint8Array, flags}; after the end {json: {t: "end"},
     *  after: true}. Rejects when the stream or channel failed. */
    next() {
      if (this._queue.length) {
        const ev = this._queue.shift();
        return new Promise((resolve) => this._hand(ev, resolve));
      }
      if (this.error) return Promise.reject(this.error);
      if (this.done || this.remoteEnded) return Promise.resolve({ json: { t: "end" }, after: true });
      return new Promise((resolve, reject) => { this._waiter = { resolve, reject }; });
    }

    /** The next JSON message, skipping DATA (which still counts as consumed). */
    async nextJson() {
      for (;;) {
        const ev = await this.next();
        if (ev.json) return ev.json;
      }
    }

    /** Count n received bytes as taken by the consumer; acks every WINDOW / 4 bytes. */
    consumed(n) {
      if (!n) return;
      this._inUnacked += n;
      if (this._inUnacked >= WINDOW / 4 && !this.remoteEnded && !this.done) {
        const a = this._inUnacked;
        this._inUnacked = 0;
        this.channel._sendJson(this.id, { t: "ack", n: a });
      }
    }

    // ---- outgoing
    _wake() {
      const c = this._credit;
      this._credit = [];
      for (const f of c) f();
    }

    async _room() {
      while (this._outUnacked >= WINDOW && !this.done && !this.channel.closed) {
        await new Promise((r) => this._credit.push(r));
      }
      if (this.channel.closed) throw this.error || this.channel.error || new Error("the channel is closed");
      if (this.done) throw this.error || new Error("the stream is closed");
    }

    sendJson(obj) {
      return this.channel._sendJson(this.id, obj);
    }

    /** Send bytes as DATA frames of at most CHUNK, waiting for the window. Resolves when the
     *  last frame was handed to the WebSocket. */
    async write(bytes) {
      const b = u8(bytes);
      let last = null;
      for (let o = 0; o < b.length; o += CHUNK) {
        await this._room();
        const piece = b.subarray(o, Math.min(o + CHUNK, b.length));
        this._outUnacked += piece.length;
        last = this.channel._send(K_DATA, this.id, piece, 0);
      }
      return last;
    }

    /** One WebSocket message (ws streams): every fragment carries TEXT for text, the last FIN. */
    async writeMessage(bytes, text) {
      const b = u8(bytes);
      const t = text ? F_TEXT : 0;
      let last = null;
      let o = 0;
      do {
        await this._room();
        const end = Math.min(o + CHUNK, b.length);
        const piece = b.subarray(o, end);
        this._outUnacked += piece.length;
        last = this.channel._send(K_DATA, this.id, piece, t | (end >= b.length ? F_FIN : 0));
        o = end;
      } while (o < b.length);
      return last;
    }

    /** No more DATA from this side. */
    end() {
      if (this.localEnded || this.done) return Promise.resolve();
      this.localEnded = true;
      const p = this.sendJson({ t: "end" });
      if (this.remoteEnded) this._finished();
      return p;
    }

    /** Give the stream up (the device drops the local connection). */
    cancel() {
      if (this.done) return;
      this.channel._sendJson(this.id, { t: "cancel" });
      this._fail(new Error("cancelled"));
    }

    /** Read and drop everything until the device ends the stream. */
    async drain() {
      try {
        for (;;) {
          const ev = await this.next();
          if (ev.after || (ev.json && (ev.json.t === "end" || TERMINAL.has(ev.json.t)))) return;
        }
      } catch (e) { /* failed: nothing to drain */ }
    }
  }

  // ---------------------------------------------------------------- HTTP transport (pipes)
  // PROTOCOL.md section 6: the same messages as a WebSocket, over plain HTTP requests, for proxies
  // that do not pass WebSockets. Down: a waiting GET (long poll) returns records
  // kind u8 | seq u32 BE | length u32 BE | payload; up: one POST at a time with records
  // kind u8 | length u32 BE | payload, the index of its first record in ?seq=.
  const PIPE_MAX_UP = 256 * 1024;  // largest up body
  const PIPE_POLL = 25;            // s a down request waits before 204
  const PIPE_IDLE = 60;            // s without a request: the relay drops the pipe
  const PIPE_BUFFER = 2 << 20;     // queued bytes per direction before the sender waits
  const P_TEXT = 1;
  const P_BINARY = 2;
  const P_CLOSE = 8;
  const WS_OPEN_TIMEOUT = 10000;   // "auto": a WebSocket that has not opened by then -> a pipe
  const CLOSE_WAIT = 3000;         // our close: how long to wait for the relay's answering close
  const RETRY = new Set([408, 425, 429, 500, 502, 503, 504]);   // worth asking again (proxies, restarts)

  /** [{kind, payload}] -> an up body. */
  function pipeEncodeUp(records) {
    let total = 0;
    for (const r of records) total += 5 + u8(r.payload).length;
    const out = new Uint8Array(total);
    const dv = new DataView(out.buffer);
    let o = 0;
    for (const r of records) {
      const p = u8(r.payload);
      dv.setUint8(o, r.kind);
      dv.setUint32(o + 1, p.length);
      out.set(p, o + 5);
      o += 5 + p.length;
    }
    return out;
  }

  function pipeDecode(bytes, withSeq) {
    const b = u8(bytes);
    const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
    const head = withSeq ? 9 : 5;
    const out = [];
    let o = 0;
    while (o < b.length) {
      if (b.length - o < head) throw new ProtocolError("a pipe record header is cut off");
      const kind = dv.getUint8(o);
      if (kind !== P_TEXT && kind !== P_BINARY && kind !== P_CLOSE) throw new ProtocolError("unknown pipe record kind " + kind);
      const seq = withSeq ? dv.getUint32(o + 1) : undefined;
      const n = dv.getUint32(o + head - 4);
      if (b.length - o - head < n) throw new ProtocolError("a pipe record is cut off");
      const payload = b.subarray(o + head, o + head + n);
      out.push(withSeq ? { kind, seq, payload } : { kind, payload });
      o += head + n;
    }
    return out;
  }

  /** A down body -> [{kind, seq, payload}]. */
  function pipeDecodeDown(bytes) { return pipeDecode(bytes, true); }
  /** An up body -> [{kind, payload}] (fake relays in tests). */
  function pipeDecodeUp(bytes) { return pipeDecode(bytes, false); }

  /** [{kind, seq, payload}] -> a down body (fake relays in tests). */
  function pipeEncodeDown(records) {
    let total = 0;
    for (const r of records) total += 9 + u8(r.payload).length;
    const out = new Uint8Array(total);
    const dv = new DataView(out.buffer);
    let o = 0;
    for (const r of records) {
      const p = u8(r.payload);
      dv.setUint8(o, r.kind);
      dv.setUint32(o + 1, r.seq >>> 0);
      dv.setUint32(o + 5, p.length);
      out.set(p, o + 9);
      o += 9 + p.length;
    }
    return out;
  }

  /** A close record's payload: code u16 BE + UTF-8 reason (at most 123 bytes, never a cut character). */
  function pipeClosePayload(code, reason) {
    let r = enc.encode(String(reason || ""));
    if (r.length > 123) r = enc.encode(dec.decode(r.subarray(0, 123)).replace(/�$/, ""));
    const out = new Uint8Array(2 + r.length);
    new DataView(out.buffer).setUint16(0, code & 0xffff);
    out.set(r, 2);
    return out;
  }

  /** A close record's payload -> {code, reason}. Empty: 1005 (no code). */
  function pipeParseClose(payload) {
    const p = u8(payload);
    if (p.length < 2) return { code: p.length ? 1002 : 1005, reason: "" };
    return { code: (p[0] << 8) | p[1], reason: dec.decode(p.subarray(2)) };
  }

  /** A WebSocket URL of the device manager -> {base: "http(s)://host/B", path: "/api/..."} for its pipe. */
  function pipeTarget(url) {
    const u = new URL(String(url), root.location && root.location.href);
    const m = /^(.*)(\/api\/(?:connect\/[^/]+|agent|agent\/data))$/.exec(u.pathname);
    if (!m) throw new Error("not a device manager connection URL: " + u.pathname);
    const proto = u.protocol === "wss:" || u.protocol === "https:" ? "https:" : "http:";
    return { base: proto + "//" + u.host + m[1], path: decodeURIComponent(m[2]) };
  }

  function domError(msg, name) {
    return typeof root.DOMException === "function" ? new root.DOMException(msg, name) : new Error(msg);
  }

  /** The WebSocket API (what Channel uses, and the usual events) over a pipe. Options (tests):
   *  {fetch, idle (ms), backoff (ms), closeWait (ms)}. */
  class PipeSocket {
    constructor(url, opts) {
      const o = opts || {};
      this.url = String(url);
      this.readyState = 0;
      this.binaryType = "blob";
      this.bufferedAmount = 0;
      this.protocol = "";
      this.extensions = "";
      this.onopen = this.onmessage = this.onclose = this.onerror = null;
      this._listeners = {};
      this._fetch = o.fetch || ((u, init) => root.fetch(u, init));
      this._idle = o.idle || PIPE_IDLE * 1000;
      this._backoff = o.backoff || 250;
      this._closeWait = o.closeWait || CLOSE_WAIT;
      const t = pipeTarget(this.url);
      this._base = t.base;
      this._path = t.path;
      this._id = null;
      this._maxUp = PIPE_MAX_UP;
      this._ack = 0;              // down messages received (= the next expected seq)
      this._up = [];              // up records not confirmed yet; the first is record number _upBase
      this._upBase = 0;
      this._done = [];            // the last confirmed batch (resent after a 409 that asks for it)
      this._upBusy = false;
      this._gone = false;         // the relay has no pipe any more: no more requests
      this._closing = null;       // {code, reason} of our close
      this._abandoned = false;    // close() while connecting
      this._down = null;          // AbortController of the waiting down request
      this._timer = null;
      this._lastOk = Date.now();
      this._openPipe();
    }

    static get CONNECTING() { return 0; }
    static get OPEN() { return 1; }
    static get CLOSING() { return 2; }
    static get CLOSED() { return 3; }

    addEventListener(type, fn) {
      if (typeof fn !== "function") return;
      const l = this._listeners[type] || (this._listeners[type] = []);
      if (!l.includes(fn)) l.push(fn);
    }

    removeEventListener(type, fn) {
      const l = this._listeners[type];
      if (l) this._listeners[type] = l.filter((f) => f !== fn);
    }

    _emit(type, extra) {
      const ev = Object.assign({ type, target: this, currentTarget: this }, extra || {});
      const on = this["on" + type];
      if (typeof on === "function") { try { on.call(this, ev); } catch (e) { setTimeout(() => { throw e; }); } }
      for (const f of (this._listeners[type] || []).slice()) {
        try { f.call(this, ev); } catch (e) { setTimeout(() => { throw e; }); }
      }
    }

    _url(rest) {
      return this._base + "/api/pipe/" + encodeURIComponent(this._id) + "/" + rest;
    }

    _ok() { this._lastOk = Date.now(); }

    /** After a failed request: true (and waited) when it is worth trying again. */
    async _again(fails) {
      if (Date.now() - this._lastOk >= this._idle) return false;
      await new Promise((r) => setTimeout(r, Math.min(this._backoff * Math.pow(2, fails), 5000)));
      return !this._gone;
    }

    async _openPipe() {
      let r, body = null;
      try {
        r = await this._fetch(this._base + "/api/pipe/open", {
          method: "POST", credentials: "same-origin", cache: "no-store",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: this._path }) });
        try { body = await r.json(); } catch (e) { body = null; }
      } catch (e) {
        this._end(1006, "the device manager is not reachable", false);
        return;
      }
      if (r.status !== 200 || !body || typeof body.pipe !== "string") {
        this._end(1006, String((body && body.error) || ("HTTP " + r.status)), false);
        return;
      }
      this._id = body.pipe;
      if (Number(body.max_up) >= 1024) this._maxUp = Number(body.max_up);
      this._ok();
      if (this._abandoned) {                     // closed while connecting: let the relay know
        this._queueClose(1000, "");
        this._end(1006, "closed before the connection was established", false);
        return;
      }
      this.readyState = 1;
      this._emit("open");
      this._downLoop();
      this._pump();
    }

    async _downLoop() {
      let fails = 0;
      while (!this._gone && this.readyState < 3) {
        const ac = typeof AbortController === "function" ? new AbortController() : null;
        this._down = ac;
        let r, buf = null;
        try {
          r = await this._fetch(this._url("down?ack=" + this._ack), {
            method: "GET", credentials: "same-origin", cache: "no-store", signal: ac ? ac.signal : undefined });
          if (r.status === 200) buf = new Uint8Array(await r.arrayBuffer());
        } catch (e) {
          if (this._gone || this.readyState === 3) return;
          if (await this._again(fails++)) continue;
          this._end(1006, "the device manager stopped answering", false);
          return;
        } finally {
          if (this._down === ac) this._down = null;
        }
        if (this._gone || this.readyState === 3) return;
        if (r.status === 204) { this._ok(); fails = 0; continue; }
        if (r.status === 410) { this._lost(); return; }
        if (r.status !== 200) {
          if (RETRY.has(r.status) && await this._again(fails++)) continue;
          this._end(1006, "the device manager answered HTTP " + r.status, false);
          return;
        }
        this._ok();
        fails = 0;
        let records;
        try { records = pipeDecodeDown(buf); } catch (e) { this._fail(1002, e.message); return; }
        for (const rec of records) {
          if (rec.seq < this._ack) continue;                      // had it already (a repeated answer)
          if (rec.seq !== this._ack) { this._fail(1002, "messages from the device manager are missing"); return; }
          this._ack++;
          if (rec.kind === P_CLOSE) {
            const c = pipeParseClose(rec.payload);
            this._gone = true;
            this._finalAck();
            this._end(c.code, c.reason, true);
            return;
          }
          if (this.readyState === 3) return;
          let data;
          if (rec.kind === P_TEXT) {
            try { data = strictDec.decode(rec.payload); } catch (e) { this._fail(1007, "a text message that is not UTF-8"); return; }
          } else if (this.binaryType === "blob" && typeof root.Blob === "function") {
            data = new root.Blob([rec.payload]);
          } else {
            data = rec.payload.slice().buffer;
          }
          this._emit("message", { data, origin: "" });
        }
      }
    }

    /** The close record arrived: acknowledge it, so the relay forgets the pipe now (it answers 410). */
    _finalAck() {
      if (!this._id) return;
      const ac = typeof AbortController === "function" ? new AbortController() : null;
      const t = setTimeout(() => { if (ac) ac.abort(); }, 5000);
      Promise.resolve()
        .then(() => this._fetch(this._url("down?ack=" + this._ack),
                                { method: "GET", credentials: "same-origin", cache: "no-store", signal: ac ? ac.signal : undefined }))
        .then((r) => r && r.body && r.body.cancel ? r.body.cancel() : null)
        .catch(() => {})
        .then(() => clearTimeout(t));
    }

    /** 410: the relay has dropped the pipe. After our close was confirmed that is the normal end. */
    _lost() {
      this._gone = true;
      if (this._closing && this._closeConfirmed) this._end(this._closing.code, this._closing.reason, true);
      else this._end(1006, "the device manager dropped the connection", false);
    }

    send(data) {
      if (this.readyState === 0) throw domError("Failed to execute 'send' on 'WebSocket': Still in CONNECTING state.", "InvalidStateError");
      let kind, bytes;
      if (typeof data === "string") { kind = P_TEXT; bytes = enc.encode(data); }
      else { kind = P_BINARY; bytes = u8(data).slice(); }      // a copy: the caller may reuse its buffer
      if (this.readyState !== 1) return;                         // closing / closed: dropped, as WebSockets do
      if (bytes.length + 5 > this._maxUp) { this._fail(1009, "a message is too large for the HTTP transport"); return; }
      this._up.push({ kind, payload: bytes });
      this.bufferedAmount += bytes.length;
      this._pump();
    }

    close(code, reason) {
      if (this.readyState >= 2) return;
      if (this.readyState === 0) {                               // as a WebSocket: fail the connection
        this.readyState = 2;
        this._abandoned = true;
        return;
      }
      this.readyState = 2;
      this._closing = { code: code === undefined ? 1005 : code, reason: reason || "" };
      this._queueClose(code === undefined ? 1000 : code, reason || "");
    }

    _queueClose(code, reason) {
      if (this._closeQueued) return;
      this._closeQueued = true;
      this._up.push({ kind: P_CLOSE, payload: pipeClosePayload(code, reason) });
      this._pump();
    }

    /** Up: one request at a time, as many queued records as fit, retried with the same seq. */
    async _pump() {
      if (this._upBusy || !this._id || this._gone) return;
      this._upBusy = true;
      let fails = 0, conflicts = 0;
      try {
        while (this._up.length && !this._gone) {
          let n = 0, size = 0;
          while (n < this._up.length && (n === 0 || size + 5 + this._up[n].payload.length <= this._maxUp)) {
            size += 5 + this._up[n].payload.length;
            n++;
          }
          const seq = this._upBase;
          let r, body = null;
          try {
            r = await this._fetch(this._url("up?seq=" + seq), {
              method: "POST", credentials: "same-origin", cache: "no-store",
              headers: { "Content-Type": "application/octet-stream" }, body: pipeEncodeUp(this._up.slice(0, n)) });
            try { body = await r.json(); } catch (e) { body = null; }
          } catch (e) {
            if (this._gone) return;
            if (await this._again(fails++)) continue;
            this._end(1006, "the device manager stopped answering", false);
            return;
          }
          if (this._gone) return;
          if (r.status === 410) { this._lost(); return; }
          if (r.status === 200 || r.status === 409) {
            const next = body && Number.isInteger(body.next) ? body.next : -1;
            if (r.status === 409 && ++conflicts > 3) {           // a relay that keeps disagreeing
              this._end(1006, "the device manager keeps refusing our messages", false);
              return;
            }
            if (next < this._upBase) {
              // 409: the relay has fewer records than it confirmed - resend from `next` if we
              // still have them (the last confirmed batch is kept for that), else give up
              const back = this._upBase - next;
              if (r.status !== 409 || back > this._done.length) {
                this._end(1006, "the device manager lost messages", false);
                return;
              }
              const again = this._done.splice(this._done.length - back, back);
              for (const rec of again) if (rec.kind !== P_CLOSE) this.bufferedAmount += rec.payload.length;
              this._up = again.concat(this._up);
              this._upBase = next;
              this._ok();
              continue;
            }
            if (next > this._upBase + n) {
              this._end(1006, "the device manager confirmed messages it was not sent", false);
              return;
            }
            this._ok();
            fails = 0;
            if (r.status === 200) conflicts = 0;
            const confirmed = this._up.splice(0, next - this._upBase);
            for (const rec of confirmed) {
              if (rec.kind === P_CLOSE) this._closeConfirmed = true;
              else this.bufferedAmount -= rec.payload.length;
            }
            if (confirmed.length) this._done = confirmed;
            this._upBase = next;
            if (this._closeConfirmed) this._afterClose();
            continue;
          }
          if (RETRY.has(r.status) && await this._again(fails++)) continue;
          this._end(1006, "the device manager answered HTTP " + r.status, false);
          return;
        }
      } finally {
        this._upBusy = false;
      }
    }

    /** Our close reached the relay: its answering close comes down (the down loop ends us then);
     *  if it does not come soon, we are done anyway. */
    _afterClose() {
      if (this.readyState === 3 || this._afterCloseTimer) return;
      const c = this._closing || { code: 1006, reason: "" };
      this._afterCloseTimer = setTimeout(() => { this._gone = true; this._end(c.code, c.reason, !!this._closing); }, this._closeWait);
    }

    /** A local failure while the pipe is open: tell the relay (close record with code), then close. */
    _fail(code, why) {
      if (this.readyState === 3) return;
      if (this._id && !this._gone) this._queueClose(code, why);
      this._end(1006, why, false);
    }

    /** CLOSED: the close event (with error first when not clean). Requests still running stop. */
    _end(code, reason, clean) {
      if (this.readyState === 3) return;
      this.readyState = 3;
      clearTimeout(this._afterCloseTimer);
      if (this._down) { try { this._down.abort(); } catch (e) { /* done */ } this._down = null; }
      if (!this._closeQueued || this._closeConfirmed) this._gone = true;     // else: the pump still delivers our close
      if (!clean) this._emit("error");
      this._emit("close", { code, reason: String(reason || ""), wasClean: !!clean });
    }
  }

  // ---------------------------------------------------------------- choosing the transport
  // "auto": a WebSocket first; when it fails before it opens, a pipe for the same route - and
  // from then on pipes for this device manager (this worker / page, and this tab's session).
  const learnedPipes = new Set();
  const PIPES_KEY = "arstro-relay-pipes ";
  const TRANSPORT_KEY = "arstro-relay-transport";

  function session() {
    try { return root.sessionStorage || null; } catch (e) { return null; }
  }

  function pipesLearned(url) {
    let base;
    try { base = pipeTarget(url).base; } catch (e) { return false; }
    if (learnedPipes.has(base)) return true;
    try { const s = session(); if (s && s.getItem(PIPES_KEY + base)) { learnedPipes.add(base); return true; } } catch (e) { /* no storage */ }
    return false;
  }

  function learnPipes(url) {
    let base;
    try { base = pipeTarget(url).base; } catch (e) { return; }
    learnedPipes.add(base);
    try { const s = session(); if (s) s.setItem(PIPES_KEY + base, "1"); } catch (e) { /* no storage */ }
  }

  function forgetPipes() {
    learnedPipes.clear();
    try {
      const s = session();
      if (s) for (let i = s.length - 1; i >= 0; i--) { const k = s.key(i); if (k && k.startsWith(PIPES_KEY)) s.removeItem(k); }
    } catch (e) { /* no storage */ }
  }

  function transportMode(opts) {
    const t = (opts && opts.transport) || (root.ArstroE2E && root.ArstroE2E.transport) || "auto";
    return t === "ws" || t === "http" ? t : "auto";
  }

  /** The transport forced for this tab (tests: sessionStorage arstro-relay-transport = ws | http). */
  function initialTransport() {
    try {
      const s = session();
      const v = s && s.getItem(TRANSPORT_KEY);
      return v === "ws" || v === "http" ? v : "auto";
    } catch (e) { return "auto"; }
  }

  // ---------------------------------------------------------------- the channel
  /** An end-to-end channel to one device. Channel.open({url, key, WebSocket}) resolves once the
   *  device's `ready` arrived; channel.info is that message. key: CryptoKey (HKDF) or raw bytes.
   *  Transport: opts.transport or ArstroE2E.transport ("auto" | "ws" | "http"); channel.transport
   *  says which one it uses. Tests: opts.fetch / opts.pipe (PipeSocket options), opts.PipeSocket. */
  class Channel {
    constructor(opts) {
      this.opts = opts || {};
      this.url = this.opts.url;
      this.info = null;
      this.closed = false;
      this.error = null;
      this.streams = new Map();
      this._nextId = 1;
      this._out = Promise.resolve();
      this._in = Promise.resolve();
      this.secure = null;
      this._phase = "relay";
      this._lastSeen = Date.now();
      this._timers = [];
      this._closeHandlers = [];
      this.sentProof = false;     // tests: nothing derived from the key left before the check
    }

    static open(opts) {
      const ch = new Channel(opts);
      return ch._open().then(() => ch);
    }

    onclose(fn) { this._closeHandlers.push(fn); }

    _open() {
      return new Promise((resolve, reject) => {
        this._resolve = resolve;
        this._reject = reject;
        try { this._connect(transportMode(this.opts)); } catch (e) { reject(e); return; }
        this._handshakeTimer();
      });
    }

    _handshakeTimer() {
      clearTimeout(this._hsTimer);
      this._hsTimer = setTimeout(() => this._fatal(new Refused("the device did not answer in time")),
                                 this.opts.timeout || HANDSHAKE_TIMEOUT);
      this._timers.push(this._hsTimer);
    }

    /** mode "ws" | "http" | "auto" (a WebSocket that fails before it opens falls back to a pipe). */
    _connect(mode) {
      const http = mode === "http" || (mode === "auto" && pipesLearned(this.url));
      let ws;
      if (http) {
        const PS = this.opts.PipeSocket || PipeSocket;
        ws = new PS(this.url, Object.assign({}, this.opts.pipe, this.opts.fetch ? { fetch: this.opts.fetch } : {}));
      } else {
        const WS = this.opts.WebSocket || root.__arstroNativeWebSocket || root.WebSocket;
        try {
          ws = new WS(this.url);
        } catch (e) {
          if (mode !== "auto") throw e;
          this._fellBack = true;
          this._connect("http");
          return;
        }
      }
      this.ws = ws;
      this.transport = http ? "http" : "ws";
      ws.binaryType = "arraybuffer";
      const fallback = mode === "auto" && !http;
      let opened = false;
      let timer = null;
      const isOpen = () => {
        if (opened) return;
        opened = true;
        clearTimeout(timer);
        if (http && this._fellBack) learnPipes(this.url);     // the WebSocket failed, the pipe works
      };
      const toPipe = () => {
        if (opened || this.closed || this.ws !== ws) return;
        clearTimeout(timer);
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
        try { ws.close(); } catch (e) { /* failed already */ }
        this._fellBack = true;
        this._connect("http");
        if (this._hsTimer) this._handshakeTimer();          // the pipe gets the whole handshake time
      };
      ws.onopen = isOpen;
      ws.onmessage = (e) => { isOpen(); this._message(e.data); };
      ws.onclose = (e) => {
        if (fallback && !opened) { toPipe(); return; }
        // through the same chain as the messages: a refusal sent just before the close wins
        this._in = this._in.then(() => this._socketClosed(e));
      };
      ws.onerror = () => {};
      if (fallback) {
        timer = setTimeout(toPipe, this.opts.wsOpenTimeout || WS_OPEN_TIMEOUT);
        this._timers.push(timer);
      }
    }

    _message(data) {
      this._lastSeen = Date.now();
      if (typeof data !== "string" && this.secure) {
        let p;
        if (data.byteLength > MAX_MESSAGE + 64) p = Promise.reject(new ProtocolError("message too large"));
        else p = this.secure.open(data);
        p.catch(() => {});
        this._in = this._in.then(() => p).then((f) => this._frame(f)).catch((e) => this._fatal(e));
      } else {
        this._in = this._in.then(() => this._text(data)).catch((e) => this._fatal(e));
      }
    }

    async _text(data) {
      if (this.closed) return;
      if (typeof data !== "string") throw new ProtocolError("unexpected binary message during the handshake");
      let msg;
      try { msg = JSON.parse(data); } catch (e) { throw new ProtocolError("bad JSON message"); }
      if (!msg || typeof msg.t !== "string") throw new ProtocolError("a message must be an object with t");
      if (this._phase === "relay") {
        if (msg.t === "error") throw new Refused(String(msg.error || "refused"));
        if (msg.t !== "relay") throw new ProtocolError("expected the relay's answer");
        if (!msg.ok) throw new Refused(String(msg.error || "the device manager refused the connection"));
        const h = viewerHello(this.opts.cn);
        this._cn = h.cn;
        this._phase = "hello";
        this.ws.send(JSON.stringify(h.msg));
        return;
      }
      if (this._phase === "hello") {
        const r = await viewerCheck(this.opts.key, this._cn, msg);   // WrongKey / Refused stop here
        if (this.closed) return;
        this.secure = r.secure;
        this._phase = "proof";
        this.sentProof = true;
        this.ws.send(JSON.stringify(r.msg));
        return;
      }
      if (msg.t === "error") {
        const e = String(msg.error || "refused");
        throw /wrong key/i.test(e) ? new WrongKey("the device refused the access key (" + e + ")") : new Refused(e);
      }
      throw new ProtocolError("a text message after the handshake");
    }

    _frame(f) {
      if (this.closed) return;
      if (f.stream === 0) {
        if (f.kind !== K_JSON) throw new ProtocolError("DATA on stream 0");
        const msg = decodeJson(f.payload);
        if (this._phase === "proof") {
          if (msg.t !== "ready") throw new ProtocolError("expected ready");
          this.info = msg;
          this._phase = "open";
          for (const t of this._timers) clearTimeout(t);
          this._timers = [];
          this._startPing();
          this._resolve(this);
          return;
        }
        if (msg.t === "ping") this._sendJson(0, { t: "pong" });
        return;
      }
      if (this._phase !== "open") throw new ProtocolError("stream frame before ready");
      const s = this.streams.get(f.stream);
      if (!s) return;                                  // a stream we gave up / that ended
      if (f.kind === K_JSON) s._json(decodeJson(f.payload));
      else if (f.kind === K_DATA) s._data(f.payload, f.flags);
      else throw new ProtocolError("unknown frame kind " + f.kind);
    }

    _startPing() {
      const every = this.opts.pingEvery || PING_EVERY;
      const iv = setInterval(() => {
        if (this.closed) return;
        if (Date.now() - this._lastSeen > (this.opts.deadAfter || DEAD_AFTER)) {
          this._fatal(new Error("the device stopped answering"));
          return;
        }
        this._sendJson(0, { t: "ping" });
      }, every);
      if (iv && iv.unref) iv.unref();
      this._timers.push(iv);
    }

    /** Seal and send in call order. Resolves when handed to the WebSocket. */
    _send(kind, stream, payload, flags) {
      if (this.closed || !this.secure) return Promise.reject(this.error || new Error("the channel is closed"));
      const p = this.secure.seal(kind, stream, payload, flags);
      this._out = this._out.then(() => p).then((ct) => {
        if (!this.closed && this.ws.readyState === 1) this.ws.send(ct);
      });
      const r = this._out;
      this._out = this._out.catch((e) => this._fatal(e));
      return r;
    }

    _sendJson(stream, obj) {
      return this._send(K_JSON, stream, enc.encode(JSON.stringify(obj)), 0).catch(() => {});
    }

    /** A new stream opened with msg ({t: "http" | "ws" | "tcp" | "ports", ...}). */
    openStream(msg) {
      if (this.closed) throw this.error || new Error("the channel is closed");
      const id = this._nextId++;
      const s = new Stream(this, id, msg.t === "http" && !msg.body);
      this.streams.set(id, s);
      this._sendJson(id, msg);
      return s;
    }

    _forget(id) {
      this.streams.delete(id);
    }

    /** -> [{port, addr, proc}] of the device's listening TCP ports. */
    async ports() {
      const s = this.openStream({ t: "ports" });
      const msg = await s.nextJson();
      if (msg.t === "error") throw new Refused(String(msg.error || "refused"));
      if (msg.t !== "ports") throw new ProtocolError("expected ports");
      return msg.ports || [];
    }

    _socketClosed(e) {
      if (this.closed) return;
      let why = "the connection to the device manager closed";
      if (this._phase === "relay" && e && e.code === 1006) why = "could not connect to the device manager (signed out, or the server is not reachable)";
      if (e && e.reason) why += ": " + e.reason;
      this._fatal(this._phase === "open" ? new Error("the connection to the device closed") : new Refused(why));
    }

    _fatal(err) {
      if (this.closed) return;
      this.closed = true;
      this.error = err;
      for (const t of this._timers) { clearTimeout(t); clearInterval(t); }
      this._timers = [];
      try { if (this.ws && this.ws.readyState <= 1) this.ws.close(); } catch (e) { /* closed */ }
      for (const s of [...this.streams.values()]) s._fail(err);
      this.streams.clear();
      if (this._reject) this._reject(err);
      this._resolve = this._reject = null;
      for (const f of this._closeHandlers) { try { f(err); } catch (x) { /* listener */ } }
    }

    close(reason) {
      this._fatal(new Error(reason || "closed"));
    }
  }

  root.ArstroE2E = {
    VERSION, LABEL, KEY_PREFIX, KEY_LEN, NONCE_LEN, CHUNK, WINDOW, MAX_MESSAGE,
    K_JSON, K_DATA, F_FIN, F_TEXT, HEADER,
    PIPE_MAX_UP, PIPE_POLL, PIPE_IDLE, PIPE_BUFFER, P_TEXT, P_BINARY, P_CLOSE,
    ProtocolError, WrongKey, Refused,
    u8, concat, b64e, b64d, equalBytes, decodeJson, nonce,
    parseKey, isKey, keyId, importKey, keyFromText,
    deriveBits, derive, proof, Secure,
    viewerHello, viewerCheck, deviceAnswer, deviceCheck,
    pipeEncodeUp, pipeDecodeUp, pipeEncodeDown, pipeDecodeDown, pipeClosePayload, pipeParseClose, pipeTarget,
    PipeSocket, pipesLearned, learnPipes, forgetPipes,
    /** "auto" (a WebSocket, a pipe when it cannot open) | "ws" | "http" - for every channel opened from now on. */
    transport: initialTransport(),
    Stream, Channel,
  };
})(typeof self !== "undefined" ? self : globalThis);
