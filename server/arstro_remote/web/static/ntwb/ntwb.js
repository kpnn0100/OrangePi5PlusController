/*
 * ntwb.js - the NTWB web SDK: what an app's web UI loads to talk to its native app
 * through Arstro Remote (NTWB-09, docs/ntwb/NTWB.md).
 *
 *   <script src="/ntwb/ntwb.js"></script>
 *   const app = NTWB.connect();                 // app id from /apps/<id>/, or {app: "id"}
 *   app.onState("model", (m) => render(m));     // retained state: called now and on change
 *   app.onEvent("saved", (data) => toast());
 *   app.onBlob("preview", (blob, header) => img.src = URL.createObjectURL(blob));
 *   const sum = await app.call("add", {a: 1, b: 2});
 *   app.notify("pointer", {x, y});              // no reply
 *   app.onStatus((s) => ...);                   // "connecting" | "starting" | "running" | "stopped" | "failed" | "offline"
 *
 * The message names and the version below are checked against the protocol definition
 * (server/arstro_remote/ntwb/spec.py) by a test - change them there first.
 */
(function (global) {
  "use strict";

  const VERSION = "1.0.0";
  const MESSAGES = {
    c2h: ["call", "notify"],
    h2c: ["ready", "status", "result", "event", "state", "error"],
  };

  class Bridge {
    constructor(opts) {
      opts = opts || {};
      const m = location.pathname.match(/^\/apps\/([^/]+)/);
      this.appId = opts.app || (m && decodeURIComponent(m[1]));
      if (!this.appId) throw new Error("NTWB.connect: no app id (not under /apps/<id>/)");
      this.status = "connecting";
      this.client = null;
      this.info = null;
      this.state = {};
      this._next = 1;
      this._pending = new Map();
      this._subs = { event: new Map(), state: new Map(), blob: new Map(), status: new Set(), ready: new Set(), error: new Set() };
      this._backoff = 500;
      this._closed = false;
      this._open();
    }

    // ------------------------------------------------------------ subscriptions
    _sub(kind, key, fn) {
      const map = this._subs[kind];
      if (!map.has(key)) map.set(key, new Set());
      map.get(key).add(fn);
      return () => map.get(key).delete(fn);
    }
    onEvent(name, fn) { return this._sub("event", name, fn); }
    onBlob(stream, fn) { return this._sub("blob", stream, fn); }
    onState(key, fn) {
      const off = this._sub("state", key, fn);
      if (key in this.state) fn(this.state[key]);
      return off;
    }
    onStatus(fn) { this._subs.status.add(fn); fn(this.status); return () => this._subs.status.delete(fn); }
    onReady(fn) { this._subs.ready.add(fn); if (this.status === "running" && this.info) fn(this.info); return () => this._subs.ready.delete(fn); }
    onError(fn) { this._subs.error.add(fn); return () => this._subs.error.delete(fn); }

    _emit(set, ...args) {
      for (const fn of [...(set || [])]) {
        try { fn(...args); } catch (e) { console.error(e); }
      }
    }
    _setStatus(s, detail) {
      if (this.status === s && !detail) return;
      this.status = s;
      this.statusDetail = detail || null;
      this._emit(this._subs.status, s, detail);
    }

    // ------------------------------------------------------------------ requests
    call(method, params, { timeout = 60000 } = {}) {
      return new Promise((resolve, reject) => {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return reject(new Error("not connected"));
        const id = "w" + this._next++;
        const timer = setTimeout(() => { this._pending.delete(id); reject(new Error(method + " timed out")); }, timeout);
        this._pending.set(id, { resolve, reject, timer });
        this._send({ t: "call", id, method, params: params || {} });
      });
    }
    notify(method, params) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this._send({ t: "notify", method, params: params || {} });
    }
    _send(msg) {
      if (!MESSAGES.c2h.includes(msg.t)) throw new Error("ntwb: a client cannot send " + msg.t);
      this.ws.send(JSON.stringify(msg));
    }

    close() { this._closed = true; if (this.ws) this.ws.close(); }

    // ------------------------------------------------------------------ transport
    _open() {
      const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/app/" + encodeURIComponent(this.appId);
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      this.ws = ws;
      ws.onmessage = (e) => (typeof e.data === "string" ? this._json(JSON.parse(e.data)) : this._blob(e.data));
      ws.onclose = () => {
        if (this.ws !== ws) return;
        for (const [, p] of this._pending) { clearTimeout(p.timer); p.reject(new Error("connection lost")); }
        this._pending.clear();
        if (this._closed) return;
        this._setStatus("offline");
        setTimeout(() => this._open(), this._backoff);
        this._backoff = Math.min(this._backoff * 2, 5000);
      };
      ws.onopen = () => { this._backoff = 500; };
    }

    _json(msg) {
      if (!MESSAGES.h2c.includes(msg.t)) { console.warn("ntwb: unexpected message", msg); return; }
      switch (msg.t) {
        case "ready":
          this.client = msg.client;
          this.info = msg.app;
          this.state = msg.state || {};
          this._setStatus("running");
          for (const [key, fns] of this._subs.state) if (key in this.state) this._emit(fns, this.state[key]);
          this._emit(this._subs.ready, msg.app);
          break;
        case "status":
          this._setStatus(msg.state === "running" && !this.client ? "starting" : msg.state, msg.detail);
          if (msg.state !== "running") this.client = null;
          break;
        case "result": {
          const p = this._pending.get(msg.id);
          if (!p) return;
          this._pending.delete(msg.id);
          clearTimeout(p.timer);
          if (msg.ok) p.resolve(msg.data); else p.reject(new Error(msg.error || "failed"));
          break;
        }
        case "event":
          this._emit(this._subs.event.get(msg.name), msg.data);
          this._emit(this._subs.event.get("*"), msg.name, msg.data);
          break;
        case "state":
          if (msg.data === null) delete this.state[msg.key]; else this.state[msg.key] = msg.data;
          this._emit(this._subs.state.get(msg.key), msg.data);
          break;
        case "error":
          console.warn("ntwb:", msg.error, msg.about || "");
          this._emit(this._subs.error, msg.error, msg.about);
          break;
      }
    }

    _blob(buf) {
      const dv = new DataView(buf);
      const n = dv.getUint16(0);
      const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 2, n)));
      const data = new Blob([new Uint8Array(buf, 2 + n)], { type: header.mime });
      this._emit(this._subs.blob.get(header.stream), data, header);
    }
  }

  global.NTWB = { VERSION, MESSAGES, connect: (opts) => new Bridge(opts), Bridge };
})(window);
