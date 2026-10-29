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

  // ---------------------------------------------------------------- the channel
  /** An end-to-end channel to one device. Channel.open({url, key, WebSocket}) resolves once the
   *  device's `ready` arrived; channel.info is that message. key: CryptoKey (HKDF) or raw bytes. */
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
        const WS = this.opts.WebSocket || root.__arstroNativeWebSocket || root.WebSocket;
        let ws;
        try { ws = new WS(this.url); } catch (e) { reject(e); return; }
        this.ws = ws;
        ws.binaryType = "arraybuffer";
        ws.onmessage = (e) => this._message(e.data);
        // through the same chain as the messages: a refusal sent just before the close wins
        ws.onclose = (e) => { this._in = this._in.then(() => this._socketClosed(e)); };
        ws.onerror = () => {};
        const t = setTimeout(() => this._fatal(new Refused("the device did not answer in time")),
                             this.opts.timeout || HANDSHAKE_TIMEOUT);
        this._timers.push(t);
      });
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
    ProtocolError, WrongKey, Refused,
    u8, concat, b64e, b64d, equalBytes, decodeJson, nonce,
    parseKey, isKey, keyId, importKey, keyFromText,
    deriveBits, derive, proof, Secure,
    viewerHello, viewerCheck, deviceAnswer, deviceCheck,
    Stream, Channel,
  };
})(typeof self !== "undefined" ? self : globalThis);
