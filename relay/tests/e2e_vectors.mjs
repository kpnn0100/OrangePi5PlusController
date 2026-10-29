#!/usr/bin/env node
// The browser viewer's crypto and pure helpers, in Node 22 (requirement-tagged).
//
//   node relay/tests/e2e_vectors.mjs [-k name] [--list]
//
// Loads relay/arstro_relay/static/e2e.js, viewer.js and shim.js as the browser does (plain
// scripts on globalThis) and checks:
//  - EVERY value of docs/relay/vectors.json (generated from server/arstro_remote/relay/e2e.py):
//    key parsing, key_id, derived keys, proofs, each frame sealed in order and opened back (RLY-03)
//  - the Channel against an in-process fake relay + device: handshake (the device's proof is
//    checked first), refusals, ordering, flow control (WINDOW), ws fragments, ping (RLY-03)
//  - the service worker's routing (PROTOCOL.md section 5), Location rewriting, the cookie jar,
//    the HTML shim injection, header mapping, and the shim's URL rewriting (RLY-22)
//  - the HTTP transport (PROTOCOL.md section 6): the pipe record codec, PipeSocket against a fake
//    relay's fetch (order, 204, retries, 409, 410, close both ways) and the Channel falling back
//    to a pipe when its WebSocket cannot open (RLY-27)

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const STATIC = path.join(ROOT, "relay", "arstro_relay", "static");
for (const f of ["e2e.js", "viewer.js", "shim.js"]) {
  vm.runInThisContext(fs.readFileSync(path.join(STATIC, f), "utf8"), { filename: path.join(STATIC, f) });
}
const E = globalThis.ArstroE2E;
const V = globalThis.ArstroViewer;
const S = globalThis.ArstroShim;
const VEC = JSON.parse(fs.readFileSync(path.join(ROOT, "docs", "relay", "vectors.json"), "utf8"));

const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const ONLY = opt("-k");
const TESTS = [];
const test = (name, reqs, fn) => TESTS.push({ name, reqs, fn });
function check(cond, msg) { if (!cond) throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg)); }
function eq(a, b, what) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) throw new Error(`${what}: expected ${sb}, got ${sa}`);
}
async function throwsLike(fn, re, what) {
  try { await fn(); } catch (e) {
    if (!re.test(e.message)) throw new Error(`${what}: wrong error "${e.message}"`);
    return e;
  }
  throw new Error(`${what}: did not throw`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hex = (b) => Buffer.from(b).toString("hex");
const enc = new TextEncoder();
const dec = new TextDecoder();

// ------------------------------------------------------------------ vectors (RLY-03)
test("vector_constants", ["RLY-03"], async () => {
  eq(E.VERSION, VEC.version, "version");
  eq(E.LABEL, VEC.label, "label");
  eq([E.CHUNK, E.WINDOW, E.MAX_MESSAGE], [65536, 524288, 1048576], "CHUNK / WINDOW / MAX_MESSAGE");
  eq([E.K_JSON, E.K_DATA, E.F_FIN, E.F_TEXT], [1, 2, 1, 2], "kinds and flags");
});

test("vector_key_parse_and_key_id", ["RLY-03"], async () => {
  const raw = E.parseKey(VEC.key);
  eq(hex(raw), hex(Uint8Array.from({ length: 32 }, (_, i) => i)), "raw key");
  eq(E.b64e(raw), VEC.key.slice(4), "base64url round trip");
  eq(await E.keyId(raw), VEC.key_id, "key_id");
  // copy-paste whitespace is ignored
  const spaced = "  " + VEC.key.slice(0, 10) + " \n" + VEC.key.slice(10, 30) + "\t" + VEC.key.slice(30) + "\n";
  eq(hex(E.parseKey(spaced)), hex(raw), "key with whitespace");
  await throwsLike(() => E.parseKey("xx1_" + VEC.key.slice(4)), /starts with ak1_/, "bad prefix");
  await throwsLike(() => E.parseKey(VEC.key.slice(0, 30)), /too short or too long/, "short key");
  await throwsLike(() => E.parseKey(VEC.key + "AAAA"), /too short or too long/, "long key");
  await throwsLike(() => E.parseKey("ak1_" + "!".repeat(43)), /do not belong/, "bad characters");
  check(E.isKey(VEC.key) && !E.isKey("ak1_abc"), "isKey");
});

test("vector_non_extractable_key", ["RLY-03"], async () => {
  const k = await E.keyFromText(VEC.key);
  eq(k.key_id, VEC.key_id, "key_id from text");
  check(k.key.extractable === false, "the stored key must be non-extractable");
  eq(k.key.algorithm.name, "HKDF", "HKDF key");
  await throwsLike(() => crypto.subtle.exportKey("raw", k.key), /extractable|not supported/i, "export must fail");
  // derivation works from the CryptoKey as from the raw bytes
  const a = await E.derive(k.key, E.b64d(VEC.cn), E.b64d(VEC.sn));
  eq(E.b64e(a.v2d), VEC.viewer_to_device_key, "v2d from CryptoKey");
});

test("vector_derived_keys_and_proofs", ["RLY-03"], async () => {
  const raw = E.parseKey(VEC.key);
  const cn = E.b64d(VEC.cn), sn = E.b64d(VEC.sn);
  const d = await E.derive(raw, cn, sn);
  eq(E.b64e(d.v2d), VEC.viewer_to_device_key, "viewer->device key");
  eq(E.b64e(d.d2v), VEC.device_to_viewer_key, "device->viewer key");
  eq(E.b64e(d.pk), VEC.proof_key, "proof key");
  eq(E.b64e(await E.proof(d.pk, "device")), VEC.proof_device, "device proof");
  eq(E.b64e(await E.proof(d.pk, "viewer")), VEC.proof_viewer, "viewer proof");
  await throwsLike(() => E.derive(raw, cn.subarray(0, 31), sn), /32 bytes/, "short cn");
});

test("vector_frames_sealed_in_order_and_opened", ["RLY-03"], async () => {
  const viewer = await E.Secure.fromRaw(E.b64d(VEC.viewer_to_device_key), E.b64d(VEC.device_to_viewer_key));
  const device = await E.Secure.fromRaw(E.b64d(VEC.device_to_viewer_key), E.b64d(VEC.viewer_to_device_key));
  // seal every frame without awaiting in between: the nonce is taken at call time
  const sealed = VEC.frames.map((f) => (f.from === "viewer" ? viewer : device).seal(f.kind, f.stream, E.b64d(f.payload), f.flags));
  const cts = await Promise.all(sealed);
  VEC.frames.forEach((f, i) => eq(E.b64e(cts[i]), f.ciphertext, `frame ${i} (${f.from}) ciphertext`));
  // open them back on the other side, in order
  const opened = await Promise.all(VEC.frames.map((f) => (f.from === "viewer" ? device : viewer).open(E.b64d(f.ciphertext))));
  VEC.frames.forEach((f, i) => {
    const o = opened[i];
    eq([o.kind, o.stream, o.flags, E.b64e(o.payload)], [f.kind, f.stream, f.flags, f.payload], `frame ${i} opened`);
  });
  eq(dec.decode(opened[1].payload), "héllo", "UTF-8 payload");
  eq(E.decodeJson(opened[2].payload), { t: "ready", v: 1 }, "ready JSON");
});

test("vector_tampered_or_reordered_frames_do_not_open", ["RLY-03"], async () => {
  const mk = () => E.Secure.fromRaw(E.b64d(VEC.device_to_viewer_key), E.b64d(VEC.viewer_to_device_key));
  const dev = VEC.frames.filter((f) => f.from === "viewer");
  const a = await mk();
  const bad = E.b64d(dev[0].ciphertext);
  bad[5] ^= 1;
  await throwsLike(() => a.open(bad), /did not decrypt/, "changed frame");
  const b = await mk();
  await throwsLike(() => b.open(E.b64d(dev[1].ciphertext)), /did not decrypt/, "second frame first (reordered)");
  await throwsLike(() => E.decodeJson(enc.encode("[1]")), /object with t/, "JSON frame without t");
  eq(hex(E.nonce(0x0102030405)), "000000000000000102030405", "nonce layout");
});

test("vector_handshake_viewer_side", ["RLY-03"], async () => {
  const raw = E.parseKey(VEC.key);
  const cn = E.b64d(VEC.cn);
  const h = E.viewerHello(cn);
  eq(h.msg, { t: "hello", v: 1, cn: VEC.cn }, "viewer hello");
  const k = await E.importKey(raw);
  const r = await E.viewerCheck(k, cn, { t: "hello", v: 1, sn: VEC.sn, proof: VEC.proof_device });
  eq(r.msg, { t: "proof", proof: VEC.proof_viewer }, "viewer proof message");
  const ct = await r.secure.seal(1, 1, E.b64d(VEC.frames[0].payload), 0);
  eq(E.b64e(ct), VEC.frames[0].ciphertext, "first frame from the handshake's Secure");
  // a device that cannot prove the key: stopped before anything derived from the key is sent
  const other = await E.importKey(new Uint8Array(32).fill(7));
  const e = await throwsLike(() => E.viewerCheck(other, cn, { t: "hello", v: 1, sn: VEC.sn, proof: VEC.proof_device }),
                             /wrong access key for this device/, "wrong key");
  check(e instanceof E.WrongKey, "WrongKey class");
  await throwsLike(() => E.viewerCheck(k, cn, { t: "error", error: "too many viewers" }), /too many viewers/, "device error");
  await throwsLike(() => E.viewerCheck(k, cn, { t: "hello", v: 2, sn: VEC.sn, proof: VEC.proof_device }), /expected hello v1/, "version");
  // and the device side twin (used by the fake device below)
  const st = await E.deviceAnswer(raw, h.msg, E.b64d(VEC.sn));
  eq(st.msg.proof, VEC.proof_device, "device answer proof");
  await E.deviceCheck(st.state, r.msg);
  await throwsLike(() => E.deviceCheck(st.state, { t: "proof", proof: VEC.proof_device }), /different access key/, "device check");
});

// ------------------------------------------------------------------ fake relay + device
// A WebSocket class for Channel({WebSocket}) whose other end is a scripted relay + device.
function makeWorld(opts = {}) {
  const raw = opts.key || E.parseKey(VEC.key);
  const world = { received: [], textFromViewer: [], maxUnacked: 0, sockets: [], pings: 0, pongs: 0 };
  class FakeWS {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.binaryType = "blob";
      world.sockets.push(this);
      setTimeout(() => { this.readyState = 1; device(this); }, 1);
    }
    send(data) {
      if (this.readyState !== 1) throw new Error("send on a closed socket");
      const d = typeof data === "string" ? data : new Uint8Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
      setTimeout(() => this.peer(d), 0);
    }
    close() {
      if (this.readyState === 3) return;
      this.readyState = 3;
      setTimeout(() => this.onclose && this.onclose({ code: 1000, reason: "" }), 0);
    }
    deliver(data) {
      if (this.readyState !== 1) return;
      const d = typeof data === "string" ? data : data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
      setTimeout(() => this.onmessage && this.onmessage({ data: d }), 0);
    }
    hangup(code = 1000, reason = "") {
      this.readyState = 3;
      setTimeout(() => this.onclose && this.onclose({ code, reason }), 0);
    }
  }

  function device(ws) {
    let phase = "hello", state = null, sec = null, chain = Promise.resolve();
    const streams = new Map();     // id -> {credit: n unacked device->viewer, waiters, onData}
    if (opts.relay === false) { ws.deliver(JSON.stringify({ t: "relay", ok: false, error: "the device is offline" })); ws.hangup(); return; }
    ws.deliver(JSON.stringify({ t: "relay", ok: true }));
    // like the Channel: the nonce is taken at seal() time, delivery keeps that order
    let out = Promise.resolve();
    const ordered = (p) => (out = out.then(() => p).then((ct) => ws.deliver(ct)));
    const sendJson = (sid, obj) => ordered(sec.sealJson(sid, obj));
    const sendData = (sid, bytes, flags = 0) => ordered(sec.seal(E.K_DATA, sid, bytes, flags));
    async function sendBody(sid, body) {
      const st = streams.get(sid);
      for (let o = 0; o < body.length; o += E.CHUNK) {
        while (st.out >= E.WINDOW) await new Promise((r) => st.waiters.push(r));
        const piece = body.subarray(o, Math.min(o + E.CHUNK, body.length));
        st.out += piece.length;
        await sendData(sid, piece);
      }
      await sendJson(sid, { t: "end" });
    }
    ws.peer = (data) => {
      chain = chain.then(async () => {
        if (typeof data === "string") {
          const msg = JSON.parse(data);
          world.textFromViewer.push(msg);
          if (phase === "hello") {
            if (opts.refuseHello) { ws.deliver(JSON.stringify({ t: "error", error: opts.refuseHello })); ws.hangup(); return; }
            const a = await E.deviceAnswer(raw, msg);
            state = a.state;
            phase = "proof";
            ws.deliver(JSON.stringify(a.msg));
          } else if (phase === "proof") {
            sec = await E.deviceCheck(state, msg);
            phase = "open";
            await sendJson(0, { t: "ready", v: 1, id: "dev-1", name: "Bench", hostname: "bench", version: "x", slot: "a",
                                ports: opts.allow || null });
            if (opts.pingFirst) await sendJson(0, { t: "ping" });
          }
          return;
        }
        const f = await sec.open(data);
        world.received.push(f);
        if (f.kind === E.K_DATA) {
          const st = streams.get(f.stream);
          st.inUnacked += f.payload.length;
          world.maxUnacked = Math.max(world.maxUnacked, st.inUnacked);
          st.onData(f.payload, f.flags);
          // the "local socket" takes the data at once: ack every WINDOW / 4
          if (st.inUnacked >= E.WINDOW / 4) { const n = st.inUnacked; st.inUnacked = 0; await sendJson(f.stream, { t: "ack", n }); }
          return;
        }
        const msg = E.decodeJson(f.payload);
        if (f.stream === 0) {
          if (msg.t === "pong") world.pongs++;
          if (msg.t === "ping") { world.pings++; await sendJson(0, { t: "pong" }); }
          return;
        }
        let st = streams.get(f.stream);
        if (msg.t === "ack") { st.out -= msg.n; const w = st.waiters; st.waiters = []; w.forEach((x) => x()); return; }
        if (msg.t === "ports") { await sendJson(f.stream, { t: "ports", ports: [{ port: 8080, addr: "0.0.0.0", proc: "python3" }, { port: 22, addr: "0.0.0.0", proc: "sshd" }] }); return; }
        if (msg.t === "http") {
          st = { out: 0, waiters: [], inUnacked: 0, body: [], onData: (p) => st.body.push(p) };
          streams.set(f.stream, st);
          st.req = msg;
          if (opts.allow && !opts.allow.includes(msg.port)) { await sendJson(f.stream, { t: "error", error: `port ${msg.port} is not allowed on this device` }); return; }
          if (!msg.body) answerHttp(f.stream, st);       // not awaited: acks must still come in
          return;
        }
        if (msg.t === "end" && st && st.req) { answerHttp(f.stream, st); return; }
        if (msg.t === "ws") {
          let parts = [];
          let echo = Promise.resolve();
          st = { out: 0, waiters: [], inUnacked: 0, onData: (p, flags) => {
            parts.push(p);
            if (flags & E.F_FIN) {
              const whole = E.concat(parts);
              parts = [];
              world.wsMessages = (world.wsMessages || []).concat([{ text: !!(flags & E.F_TEXT), size: whole.length }]);
              // echo, in fragments of 1000 bytes, one message after the other
              echo = echo.then(async () => {
                for (let o = 0; o < whole.length || o === 0; o += 1000) {
                  const end = Math.min(o + 1000, whole.length);
                  await sendData(f.stream, whole.subarray(o, end), (flags & E.F_TEXT) | (end >= whole.length ? E.F_FIN : 0));
                  if (end >= whole.length) break;
                }
              });
            }
          } };
          streams.set(f.stream, st);
          await sendJson(f.stream, { t: "open", protocol: (msg.protocols || [])[0] || "" });
          return;
        }
        if (msg.t === "close" && st) { await sendJson(f.stream, { t: "close", code: msg.code, reason: "bye" }); streams.delete(f.stream); }
      }).catch((e) => { world.deviceError = e; ws.hangup(1011, "device error"); });
    };

    function answerHttp(sid, st) {
      doAnswer(sid, st).catch((e) => { world.deviceError = e; });
    }
    async function doAnswer(sid, st) {
      const body = E.concat(st.body);
      world.requestBody = body;
      const size = Number((st.req.headers.find(([k]) => k === "x-size") || [0, 0])[1]) || 0;
      const out = new Uint8Array(size);
      for (let i = 0; i < size; i++) out[i] = (i * 7) & 255;
      await sendJson(sid, { t: "res", status: 200, reason: "OK", headers: [["content-type", "application/octet-stream"],
                                                                            ["x-got", String(body.length)]] });
      await sendBody(sid, out);
    }
  }
  world.WebSocket = FakeWS;
  world.device = device;
  return world;
}

async function channel(world, key = E.parseKey(VEC.key), extra = {}) {
  return E.Channel.open(Object.assign({ url: "ws://relay.test/b/api/connect/dev-1", key, WebSocket: world.WebSocket }, extra));
}

test("channel_handshake_ready_and_ports", ["RLY-03", "RLY-04"], async () => {
  const w = makeWorld();
  const ch = await channel(w, await E.importKey(E.parseKey(VEC.key)));
  eq([ch.info.t, ch.info.name, ch.info.ports], ["ready", "Bench", null], "ready");
  eq(w.textFromViewer.map((m) => m.t), ["hello", "proof"], "handshake messages");
  const ports = await ch.ports();
  eq(ports.map((p) => p.port), [8080, 22], "ports");
  check(!ch.streams.size, "the ports stream ends with its answer");
  ch.close();
  check(w.sockets[0].readyState === 3, "socket closed");
});

test("channel_wrong_key_stops_before_the_proof", ["RLY-03"], async () => {
  const w = makeWorld();
  const e = await throwsLike(() => channel(w, new Uint8Array(32).fill(9)), /wrong access key for this device/, "wrong key");
  check(e instanceof E.WrongKey, "WrongKey");
  eq(w.textFromViewer.map((m) => m.t), ["hello"], "nothing derived from the key was sent");
  await sleep(10);
  check(w.sockets[0].readyState === 3, "socket closed after a wrong key");
});

test("channel_refusals_are_reported", ["RLY-03"], async () => {
  await throwsLike(() => channel(makeWorld({ relay: false })), /the device is offline/, "relay refusal");
  await throwsLike(() => channel(makeWorld({ refuseHello: "too many wrong keys - wait a minute" })), /too many wrong keys/, "device refusal");
});

test("channel_http_flow_control_both_ways", ["RLY-03", "RLY-04"], async () => {
  const w = makeWorld();
  const ch = await channel(w);
  const up = new Uint8Array(1_500_000);
  for (let i = 0; i < up.length; i++) up[i] = (i * 13) & 255;
  const down = 2_200_000;
  const s = ch.openStream({ t: "http", method: "POST", port: 8080, path: "/up", headers: [["x-size", String(down)]], body: true });
  const writing = s.write(up).then(() => s.end());
  const head = await s.nextJson();
  eq([head.t, head.status], ["res", 200], "response head");
  // read slowly at first: the device must stop at WINDOW until we take data (ack on pull)
  await sleep(50);
  const st = s;
  check(st._queue.filter((e) => e.data).reduce((n, e) => n + e.data.length, 0) <= E.WINDOW, "device exceeded the window");
  let got = 0, ok = true;
  for (;;) {
    const ev = await s.next();
    if (ev.data) { for (let i = 0; i < ev.data.length; i++) if (ev.data[i] !== ((got + i) * 7 & 255)) ok = false; got += ev.data.length; continue; }
    if (ev.json.t === "end") break;
  }
  await writing;
  eq(got, down, "response bytes");
  check(ok, "response content");
  eq(w.requestBody.length, up.length, "request bytes at the device");
  check(hex(w.requestBody.subarray(0, 64)) === hex(up.subarray(0, 64)) && w.requestBody[up.length - 1] === up[up.length - 1], "request content");
  check(w.maxUnacked <= E.WINDOW, "viewer exceeded the window: " + w.maxUnacked);
  check(w.received.filter((f) => f.kind === E.K_DATA).every((f) => f.payload.length <= E.CHUNK), "DATA larger than CHUNK");
  check(!ch.streams.size, "stream removed after both ends");
  ch.close();
});

test("channel_many_concurrent_streams_keep_nonce_order", ["RLY-03"], async () => {
  const w = makeWorld();
  const ch = await channel(w);
  const all = [];
  for (let i = 0; i < 40; i++) {
    const s = ch.openStream({ t: "http", method: "GET", port: 8080, path: "/" + i, headers: [["x-size", String(1000 + i)]], body: false });
    all.push((async () => {
      const head = await s.nextJson();
      let n = 0;
      for (;;) { const ev = await s.next(); if (ev.data) n += ev.data.length; else if (ev.json.t === "end") break; }
      return [head.status, n];
    })());
  }
  const res = await Promise.all(all);
  res.forEach(([st, n], i) => eq([st, n], [200, 1000 + i], "stream " + i));
  check(!w.deviceError, "device failed: " + (w.deviceError && w.deviceError.message));
  ch.close();
});

test("channel_ws_messages_span_frames", ["RLY-03"], async () => {
  const w = makeWorld();
  const ch = await channel(w);
  const s = ch.openStream({ t: "ws", port: 8080, path: "/ws", headers: [], protocols: ["arstro"] });
  eq(await s.nextJson(), { t: "open", protocol: "arstro" }, "open");
  const big = "x".repeat(E.CHUNK * 2 + 5) + "é";
  await s.writeMessage(enc.encode(big), true);
  await s.writeMessage(new Uint8Array(0), false);
  // the device echoes in 1000-byte fragments; put them back together
  const msgs = [];
  let parts = [], text = false;
  while (msgs.length < 2) {
    const ev = await s.next();
    if (!ev.data) continue;
    parts.push(ev.data);
    if (ev.flags & E.F_TEXT) text = true;
    if (ev.flags & E.F_FIN) { msgs.push({ text, data: E.concat(parts) }); parts = []; text = false; }
  }
  eq(dec.decode(msgs[0].data) === big && msgs[0].text, true, "text message round trip");
  eq([msgs[1].text, msgs[1].data.length], [false, 0], "empty binary message");
  eq(w.wsMessages, [{ text: true, size: enc.encode(big).length }, { text: false, size: 0 }], "device saw whole messages");
  const fr = w.received.filter((f) => f.kind === E.K_DATA && f.stream === s.id);
  eq(fr.map((f) => f.flags), [E.F_TEXT, E.F_TEXT, E.F_TEXT | E.F_FIN, E.F_FIN], "fragment flags");
  await s.sendJson({ t: "close", code: 1000, reason: "" });
  eq((await s.nextJson()).t, "close", "close answered");
  ch.close();
});

test("channel_ping_pong_and_bad_frame_ends_it", ["RLY-03"], async () => {
  const w = makeWorld({ pingFirst: true });
  const ch = await channel(w, undefined, { pingEvery: 20 });
  await sleep(80);
  check(w.pongs >= 1, "the viewer answered the device's ping");
  check(w.pings >= 1, "the viewer pings");
  let closedWith = null;
  ch.onclose((e) => { closedWith = e; });
  const s = ch.openStream({ t: "ports" });
  const p = s.nextJson();
  w.sockets[0].deliver(new Uint8Array(40));          // not a frame of this channel
  await throwsLike(() => p, /did not decrypt/, "pending stream fails");
  check(ch.closed && closedWith && /did not decrypt/.test(closedWith.message), "channel closed on a bad frame");
});

// ------------------------------------------------------------------ HTTP transport (RLY-27)
// A fake relay behind fetch(): POST B/api/pipe/open, GET .../down?ack=n, POST .../up?seq=n, as
// PROTOCOL.md section 6 (and relay/arstro_relay/pipe.py). R.fault(method, url, init) may return
// "network" (the request never arrives), "lost" (it is handled, the answer is lost) or a Response.
function makePipeRelay(opts = {}) {
  const R = { pipes: new Map(), log: [], upSizes: [], maxUp: opts.maxUp || E.PIPE_MAX_UP, poll: opts.poll || 40,
              fault: null, onOpen: opts.onOpen || null, onUp: opts.onUp || null, refuse: null };
  const json = (status, obj) => new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
  let n = 0;
  function push(pipe, kind, payload) {
    if (pipe.closeSeq !== null) return;
    if (kind === E.P_CLOSE) pipe.closeSeq = pipe.seq;
    pipe.down.push({ kind, seq: pipe.seq++, payload: E.u8(payload) });
    if (pipe.poller && pipe.poller.wake) pipe.poller.wake();
  }
  async function handle(method, u, init) {
    if (u.pathname.endsWith("/api/pipe/open")) {
      if (R.refuse) return json(R.refuse[0], { error: R.refuse[1] });
      const pipe = { id: "pipe" + (++n) + "-x", path: JSON.parse(init.body).path, down: [], seq: 0, closeSeq: null,
                     upCount: 0, ups: [], closedBy: null, gone: false, poller: null };
      pipe.push = (kind, payload) => push(pipe, kind, payload);
      R.pipes.set(pipe.id, pipe);
      if (R.onOpen) R.onOpen(pipe);
      return json(200, { pipe: pipe.id, max_up: R.maxUp, poll: 25 });
    }
    const m = /\/api\/pipe\/([^/]+)\/(down|up)$/.exec(u.pathname);
    const pipe = m && R.pipes.get(decodeURIComponent(m[1]));
    if (!pipe || pipe.gone) return json(410, { error: "this pipe is closed" });
    if (m[2] === "down") {
      const ack = Number(u.searchParams.get("ack"));
      if (ack > pipe.seq) return json(400, { error: "bad ack" });
      while (pipe.down.length && pipe.down[0].seq < ack) pipe.down.shift();
      if (pipe.closeSeq !== null && ack > pipe.closeSeq) { pipe.gone = true; return json(410, { error: "this pipe is closed" }); }
      if (pipe.poller) pipe.poller.supersede();
      const me = {};
      pipe.poller = me;
      if (!pipe.down.length) {
        await new Promise((resolve) => {
          const t = setTimeout(resolve, R.poll);
          me.wake = () => { clearTimeout(t); setTimeout(resolve, 0); };       // messages queued right now go along
          me.supersede = () => { me.superseded = true; clearTimeout(t); resolve(); };
          if (init.signal) init.signal.addEventListener("abort", () => { me.aborted = true; clearTimeout(t); resolve(); });
        });
      }
      if (pipe.poller === me) pipe.poller = null;
      if (me.aborted) throw new DOMException("The operation was aborted.", "AbortError");
      if (pipe.gone) return json(410, { error: "this pipe is closed" });
      if (me.superseded || !pipe.down.length) return new Response(null, { status: 204 });
      R.downAnswers = (R.downAnswers || 0) + 1;
      return new Response(E.pipeEncodeDown(pipe.down), { status: 200, headers: { "content-type": "application/octet-stream" } });
    }
    const body = E.u8(init.body);
    R.upSizes.push(body.length);
    if (body.length > R.maxUp) return json(413, { error: "too large" });
    const seq = Number(u.searchParams.get("seq"));
    if (seq > pipe.upCount) return json(409, { next: pipe.upCount });
    E.pipeDecodeUp(body).forEach((rec, i) => {
      if (seq + i < pipe.upCount) return;                  // applied before (a retried request)
      pipe.upCount++;
      if (pipe.closedBy) return;
      pipe.ups.push(rec);
      if (rec.kind === E.P_CLOSE) {
        pipe.closedBy = E.pipeParseClose(rec.payload);
        push(pipe, E.P_CLOSE, E.pipeClosePayload(pipe.closedBy.code === 1005 ? 1000 : pipe.closedBy.code, ""));   // the answering close
      } else if (R.onUp) R.onUp(pipe, rec);
    });
    return json(200, { next: pipe.upCount });
  }
  R.fetch = async (url, init = {}) => {
    const u = new URL(url);
    const method = init.method || "GET";
    R.log.push(method + " " + u.pathname.replace(/^.*\/api\/pipe\//, "") + u.search);
    const f = R.fault ? R.fault(method, u, init) : null;
    if (f === "network") { await sleep(1); throw new TypeError("fetch failed"); }
    if (f && f !== "lost") return f;
    const r = await handle(method, u, init);
    if (f === "lost") throw new TypeError("fetch failed (the answer was lost)");
    return r;
  };
  return R;
}

/** An echo pipe: every up message comes back down. */
function echoRelay(opts = {}) {
  return makePipeRelay(Object.assign({ onUp: (pipe, rec) => pipe.push(rec.kind, rec.payload) }, opts));
}

/** -> {ws, events, opened, closed (Promise of the close event)} */
function pipeSocket(R, extra = {}) {
  const ws = new E.PipeSocket("ws://relay.test/b/api/connect/dev-1", Object.assign({ fetch: R.fetch, backoff: 5 }, extra));
  const events = [];
  const out = { ws, events, messages: [] };
  out.opened = new Promise((resolve) => { ws.onopen = () => { events.push("open"); resolve(); }; });
  out.closed = new Promise((resolve) => { ws.addEventListener("close", (e) => { events.push("close"); resolve(e); }); });
  ws.onerror = () => events.push("error");
  ws.onmessage = (e) => out.messages.push(e.data);
  return out;
}

test("pipe_constants_and_record_codec", ["RLY-27"], async () => {
  const py = fs.readFileSync(path.join(ROOT, "server", "arstro_remote", "relay", "e2e.py"), "utf8");
  for (const [name, value] of [["PIPE_MAX_UP", 262144], ["PIPE_POLL", 25], ["PIPE_IDLE", 60], ["PIPE_BUFFER", 2097152]]) {
    eq(E[name], value, name);
    const m = new RegExp("^" + name + "\\s*=\\s*([^#\\n]+)", "m").exec(py);
    check(m, name + " missing in e2e.py");
    // e.g. "256 * 1024", "2 << 20": evaluate the small arithmetic without eval
    const expr = m[1].trim();
    const v = /^(\d+)\s*\*\s*(\d+)$/.test(expr) ? expr.split("*").reduce((a, b) => a * Number(b), 1)
      : /^(\d+)\s*<<\s*(\d+)$/.test(expr) ? Number(expr.split("<<")[0]) * 2 ** Number(expr.split("<<")[1]) : Number(expr);
    eq(v, value, name + " in e2e.py");
  }
  // up: kind u8 | length u32 BE | payload
  const up = E.pipeEncodeUp([{ kind: 1, payload: enc.encode("hé") }, { kind: 2, payload: new Uint8Array([0, 255]) },
                             { kind: 8, payload: E.pipeClosePayload(1000, "bye") }]);
  eq(hex(up), "01" + "00000003" + "68c3a9" + "02" + "00000002" + "00ff" + "08" + "00000005" + "03e8" + "627965", "up bytes");
  eq(E.pipeDecodeUp(up).map((r) => [r.kind, hex(r.payload)]), [[1, "68c3a9"], [2, "00ff"], [8, "03e8627965"]], "up decoded");
  // down: kind u8 | seq u32 BE | length u32 BE | payload
  const down = E.pipeEncodeDown([{ kind: 2, seq: 7, payload: new Uint8Array([9]) }, { kind: 1, seq: 0x01020304, payload: new Uint8Array(0) }]);
  eq(hex(down), "02" + "00000007" + "00000001" + "09" + "01" + "01020304" + "00000000", "down bytes");
  eq(E.pipeDecodeDown(down).map((r) => [r.kind, r.seq, r.payload.length]), [[2, 7, 1], [1, 0x01020304, 0]], "down decoded");
  eq(E.pipeDecodeDown(new Uint8Array(0)), [], "empty body");
  await throwsLike(() => E.pipeDecodeDown(down.subarray(0, 12)), /cut off/, "cut record");
  await throwsLike(() => E.pipeDecodeDown(down.subarray(0, 5)), /header is cut off/, "cut header");
  await throwsLike(() => E.pipeDecodeUp(Uint8Array.from([3, 0, 0, 0, 0])), /unknown pipe record kind 3/, "bad kind");
  // close payloads: code u16 BE + UTF-8 reason (<= 123 bytes, never a cut character)
  eq(E.pipeParseClose(E.pipeClosePayload(4001, "done")), { code: 4001, reason: "done" }, "close round trip");
  eq(E.pipeParseClose(new Uint8Array(0)), { code: 1005, reason: "" }, "empty close = no code");
  const long = E.pipeClosePayload(1000, "é".repeat(100));
  check(long.length - 2 <= 123 && E.pipeParseClose(long).reason === "é".repeat(61), "close reason cut at a character");
  // the pipe of a WebSocket route
  eq(E.pipeTarget("wss://relay.example.com/arstro/internal/devices/api/connect/bench-1"),
     { base: "https://relay.example.com/arstro/internal/devices", path: "/api/connect/bench-1" }, "viewer route");
  eq(E.pipeTarget("ws://localhost:8090/x/api/agent/data"), { base: "http://localhost:8090/x", path: "/api/agent/data" }, "data route");
  eq(E.pipeTarget("ws://h/api/connect/api"), { base: "http://h", path: "/api/connect/api" }, "empty base");
  await throwsLike(() => E.pipeTarget("ws://h/b/api/devices"), /not a device manager connection URL/, "other route");
});

test("pipe_socket_order_204_and_close_both_ways", ["RLY-27"], async () => {
  const R = echoRelay({ poll: 20 });
  const p = pipeSocket(R);
  check(p.ws.readyState === 0, "CONNECTING first");
  await throwsLike(() => p.ws.send("x"), /CONNECTING/, "send while connecting");
  await p.opened;
  check(p.ws.readyState === 1, "OPEN");
  p.ws.binaryType = "arraybuffer";
  await sleep(60);                                          // nothing to say: down answers 204, re-polled at once
  const polls = R.log.filter((l) => l.includes("/down?ack=0")).length;
  check(polls >= 2, "the down request is re-polled after 204: " + polls);
  const sent = [];
  for (let i = 0; i < 300; i++) {
    if (i % 3) { const b = new Uint8Array(1 + (i * 37) % 3000).fill(i & 255); sent.push(hex(b)); p.ws.send(b); }
    else { const t = "m" + i + "é".repeat(i % 50); sent.push(t); p.ws.send(t); }
  }
  check(p.ws.bufferedAmount > 0, "bufferedAmount counts what is not confirmed yet");
  for (let i = 0; i < 200 && p.messages.length < sent.length; i++) await sleep(10);
  eq(p.messages.map((m) => (typeof m === "string" ? m : hex(new Uint8Array(m)))).join(","), sent.join(","), "messages in order, text and binary");
  check(p.messages.some((m) => m instanceof ArrayBuffer), "binaryType arraybuffer");
  eq(p.ws.bufferedAmount, 0, "bufferedAmount after the relay confirmed everything");
  check(R.upSizes.length < 300, "up messages are batched: " + R.upSizes.length + " requests for 300 messages");
  // the relay closes: close record down -> close event, then the final ack frees the pipe (410)
  const pipe = [...R.pipes.values()][0];
  pipe.push(E.P_CLOSE, E.pipeClosePayload(4001, "done"));
  const ev = await p.closed;
  eq([ev.code, ev.reason, ev.wasClean, p.ws.readyState], [4001, "done", true, 3], "close from the relay");
  eq(p.events, ["open", "close"], "no error event on a clean close");
  await sleep(20);
  check(pipe.gone && R.log.some((l) => l.endsWith("/down?ack=" + pipe.seq)), "the close was acknowledged: " + R.log.slice(-2));

  // our close: close record up, the relay's answering close comes down
  const R2 = echoRelay({ poll: 20 });
  const q = pipeSocket(R2);
  await q.opened;
  q.ws.send("last");
  q.ws.close(3001, "bye");
  check(q.ws.readyState === 2, "CLOSING");
  q.ws.send("dropped");                                     // after close(): dropped, as WebSockets do
  const ev2 = await q.closed;
  const p2 = [...R2.pipes.values()][0];
  eq(p2.ups.map((r) => r.kind), [E.P_TEXT, E.P_CLOSE], "up: the message, then the close");
  eq(p2.closedBy, { code: 3001, reason: "bye" }, "close code and reason reached the relay");
  eq([ev2.code, ev2.wasClean], [3001, true], "close event");
  eq(q.messages, ["last"], "messages before the close still arrive");
  await sleep(20);
  check(p2.gone, "the pipe is released after the close");
  // the default binaryType is blob, as for a WebSocket
  const R3 = echoRelay();
  const b = pipeSocket(R3);
  await b.opened;
  b.ws.send(new Uint8Array([1, 2, 3]));
  for (let i = 0; i < 100 && !b.messages.length; i++) await sleep(5);
  check(b.messages[0] instanceof Blob && b.messages[0].size === 3, "Blob by default");
  b.ws.close();
  await b.closed;
});

test("pipe_socket_retries_resends_after_409_and_batches_up_to_max_up", ["RLY-27"], async () => {
  // up: one request at a time, batches <= max_up, a lost answer is retried with the same seq
  const R = echoRelay({ maxUp: 4000 });
  let ups = 0;
  R.fault = (method, u) => {
    if (method !== "POST" || !u.pathname.endsWith("/up")) return null;
    ups++;
    if (ups === 2) return "network";                        // never arrives
    if (ups === 4) return "lost";                           // applied, the answer is lost: retried, applied once
    if (ups === 6) return new Response('{"error":"bad gateway"}', { status: 502 });
    return null;
  };
  const p = pipeSocket(R);
  await p.opened;
  p.ws.binaryType = "arraybuffer";
  const sent = [];
  for (let i = 0; i < 120; i++) { const b = new Uint8Array(100 + i * 20).fill(i); sent.push(b.length + ":" + i); p.ws.send(b); }
  for (let i = 0; i < 300 && p.messages.length < sent.length; i++) await sleep(10);
  eq(p.messages.map((m) => m.byteLength + ":" + new Uint8Array(m)[0]), sent, "every message once, in order");
  check(R.upSizes.every((n) => n <= 4000), "an up body over max_up: " + Math.max(...R.upSizes));
  check(R.upSizes.some((n) => n > 2500), "batches fill up to max_up");
  const pipe = [...R.pipes.values()][0];
  eq(pipe.upCount, 120, "applied once each");
  const seqs = R.log.filter((l) => l.startsWith("POST") && l.includes("/up?")).map((l) => Number(l.split("seq=")[1]));
  check(seqs.some((v, i) => i && v === seqs[i - 1]), "a failed up request is retried with the same seq: " + seqs.join(","));
  await throwsLike(async () => { p.ws.send(new Uint8Array(4000)); await p.closed; throw new Error(String((await p.closed).code)); },
                   /1006/, "a message larger than max_up ends the pipe");

  // 409: the relay has fewer records than it confirmed -> resend from `next`
  const R2 = makePipeRelay({ poll: 20 });
  const q = pipeSocket(R2);
  await q.opened;
  q.ws.send("a");
  q.ws.send("b");
  for (let i = 0; i < 100 && (q.ws.bufferedAmount || R2.pipes.values().next().value.upCount < 2); i++) await sleep(5);
  const p2 = [...R2.pipes.values()][0];
  eq(p2.upCount, 2, "a and b confirmed");
  p2.upCount = 1;                                           // the relay "lost" b
  p2.ups.pop();
  q.ws.send("c");
  for (let i = 0; i < 100 && p2.ups.length < 3; i++) await sleep(5);
  eq(p2.ups.map((r) => dec.decode(r.payload)), ["a", "b", "c"], "resent from next after 409");
  check(R2.log.some((l) => l.includes("/up?seq=1")), "resent with seq=next");
  eq(q.ws.readyState, 1, "still open");
  // a 409 asking for more than the last confirmed batch cannot be served: the pipe ends
  p2.upCount = 0;
  p2.ups = [];
  q.ws.send("d");
  const ev = await q.closed;
  eq([ev.code, ev.wasClean], [1006, false], "lost messages end the pipe");
  check(q.events.includes("error"), "error before close");
});

test("pipe_socket_down_retries_410_idle_and_refused_open", ["RLY-27"], async () => {
  // down: network errors and 502 are retried (same ack: nothing lost or doubled)
  const R = echoRelay({ poll: 20 });
  let downs = 0;
  R.fault = (method, u) => {
    if (!u.pathname.endsWith("/down")) return null;
    downs++;
    if (downs >= 2 && downs <= 4) return "network";
    if (downs === 6) return "lost";
    if (downs === 7) return new Response("", { status: 504 });
    return null;
  };
  const p = pipeSocket(R);
  await p.opened;
  for (let i = 0; i < 10; i++) p.ws.send("n" + i);
  for (let i = 0; i < 300 && downs < 9; i++) await sleep(5);          // 204s, failures and retries meanwhile
  check(downs >= 9, "down requests retried: " + downs);
  for (let i = 10; i < 30; i++) p.ws.send("n" + i);
  for (let i = 0; i < 300 && p.messages.length < 30; i++) await sleep(5);
  eq(p.messages, Array.from({ length: 30 }, (_, i) => "n" + i), "in order after down retries");
  // 410 while open: the relay dropped the pipe -> error + close 1006
  [...R.pipes.values()][0].gone = true;
  const ev = await p.closed;
  eq([ev.code, ev.wasClean, p.events.slice(-2)], [1006, false, ["error", "close"]], "410");

  // no answer for longer than PIPE_IDLE (here 80 ms): the pipe is given up
  const R2 = echoRelay({ poll: 20 });
  const q = pipeSocket(R2, { idle: 80 });
  await q.opened;
  R2.fault = () => "network";
  const t0 = Date.now();
  const ev2 = await q.closed;
  check(ev2.code === 1006 && Date.now() - t0 < 2000, "idle: " + ev2.code + " after " + (Date.now() - t0) + " ms");

  // open refused (signed out, rate limit, ...): error + close 1006 with the relay's reason
  const R3 = echoRelay();
  R3.refuse = [401, "log in first"];
  const r = pipeSocket(R3);
  const ev3 = await r.closed;
  eq([ev3.code, ev3.reason, r.events], [1006, "log in first", ["error", "close"]], "refused open");
  // close() while connecting: no open event, the relay is told (close record)
  const R4 = echoRelay();
  const c = pipeSocket(R4);
  c.ws.close();
  const ev4 = await c.closed;
  await sleep(20);
  eq([ev4.code, c.events.includes("open")], [1006, false], "closed while connecting");
  eq([...R4.pipes.values()][0].closedBy && [...R4.pipes.values()][0].closedBy.code, 1000, "the relay got the close");
});

function deviceRelay(world, opts = {}) {
  return makePipeRelay(Object.assign({
    onOpen: (pipe) => {
      const sock = {
        readyState: 1,
        deliver(data) { if (this.readyState === 1) pipe.push(typeof data === "string" ? E.P_TEXT : E.P_BINARY, typeof data === "string" ? enc.encode(data) : data); },
        hangup(code = 1000, reason = "") { this.readyState = 3; pipe.push(E.P_CLOSE, E.pipeClosePayload(code, reason)); },
      };
      pipe.sock = sock;
      world.device(sock);
    },
    onUp: (pipe, rec) => pipe.sock.peer(rec.kind === E.P_TEXT ? dec.decode(rec.payload) : rec.payload.slice()),
  }, opts));
}

test("channel_falls_back_to_a_pipe_and_remembers_it", ["RLY-27", "RLY-03", "RLY-04"], async () => {
  E.forgetPipes();
  const w = makeWorld();
  const R = deviceRelay(w, { maxUp: 200000, poll: 30 });
  let tries = 0;
  class DeadWS {                                            // what an nginx without Upgrade lines gives: no open, close 1006
    constructor() { tries++; this.readyState = 0; setTimeout(() => { this.readyState = 3; if (this.onerror) this.onerror({}); if (this.onclose) this.onclose({ code: 1006, reason: "" }); }, 2); }
    send() { throw new Error("not open"); }
    close() {}
  }
  const url = "ws://relay.test/b/api/connect/dev-1";
  const ch = await E.Channel.open({ url, key: E.parseKey(VEC.key), WebSocket: DeadWS, fetch: R.fetch, pipe: { backoff: 5 } });
  eq([tries, ch.transport, ch.info.name], [1, "http", "Bench"], "WebSocket tried once, then the pipe");
  eq([...R.pipes.values()][0].path, "/api/connect/dev-1", "the pipe is for the same route");
  check(E.pipesLearned(url), "the device manager is remembered as needing pipes");
  eq((await ch.ports()).map((x) => x.port), [8080, 22], "ports over the pipe");
  // a body both ways through the window, in order, over batched up requests
  const up = new Uint8Array(700_000);
  for (let i = 0; i < up.length; i++) up[i] = (i * 13) & 255;
  const s = ch.openStream({ t: "http", method: "POST", port: 8080, path: "/up", headers: [["x-size", "900000"]], body: true });
  const writing = s.write(up).then(() => s.end());
  eq((await s.nextJson()).status, 200, "response head");
  let got = 0, ok = true;
  for (;;) {
    const ev = await s.next();
    if (ev.data) { for (let i = 0; i < ev.data.length; i++) if (ev.data[i] !== ((got + i) * 7 & 255)) ok = false; got += ev.data.length; continue; }
    if (ev.json.t === "end") break;
  }
  await writing;
  check(got === 900000 && ok, "response body over the pipe: " + got);
  check(w.requestBody.length === up.length && w.requestBody[up.length - 1] === up[up.length - 1], "request body over the pipe");
  check(w.maxUnacked <= E.WINDOW, "window");
  check(R.upSizes.every((n) => n <= 200000), "up bodies within max_up");
  ch.close();
  await sleep(50);
  const p0 = [...R.pipes.values()][0];
  check(p0.closedBy && p0.gone, "closing the channel closes and releases the pipe");
  // the next channel skips the WebSocket
  const ch2 = await E.Channel.open({ url, key: E.parseKey(VEC.key), WebSocket: DeadWS, fetch: R.fetch, pipe: { backoff: 5 } });
  eq([tries, ch2.transport], [1, "http"], "no second WebSocket attempt");
  ch2.close();
  E.forgetPipes();
  check(!E.pipesLearned(url), "forgotten");
  // a working WebSocket: no pipe at all; "http" forced: no WebSocket at all
  const opens = () => R.log.filter((l) => l === "POST open").length;
  const before = opens();
  check(before === 2, "pipes opened so far: " + before);
  const ch3 = await channel(makeWorld(), undefined, { fetch: R.fetch });
  eq([ch3.transport, opens()], ["ws", before], "auto with a working WebSocket");
  ch3.close();
  const ch4 = await E.Channel.open({ url, key: E.parseKey(VEC.key), WebSocket: DeadWS, fetch: R.fetch, transport: "http" });
  eq([tries, ch4.transport], [1, "http"], "forced http");
  ch4.close();
  check(!E.pipesLearned(url), "a forced transport is not remembered");
  // refusals still come through: the relay's answer, or the pipe's open refused
  const W5 = makeWorld({ relay: false });
  await throwsLike(() => E.Channel.open({ url, key: E.parseKey(VEC.key), fetch: deviceRelay(W5).fetch, transport: "http" }),
                   /the device is offline/, "relay refusal over a pipe");
  const R6 = deviceRelay(makeWorld());
  R6.refuse = [401, "log in first"];
  await throwsLike(() => E.Channel.open({ url, key: E.parseKey(VEC.key), WebSocket: DeadWS, fetch: R6.fetch }),
                   /could not connect to the device manager.*log in first/, "pipe open refused");
  check(!E.pipesLearned(url), "a refused pipe is not remembered");
});

// ------------------------------------------------------------------ routing (RLY-22)
const B = "/arstro/internal/devices";
const ORIGIN = "https://relay.example.com";

test("viewer_base_path_from_files", ["RLY-22"], async () => {
  eq(V.baseOf(B + "/sw.js", "sw.js"), B, "from sw.js");
  eq(V.baseOf(B + "/static/app.js", "static/app.js"), B, "from app.js");
  eq(V.baseOf("/x/static/boot.js", "static/boot.js"), "/x", "other base");
});

test("viewer_routing_rules_section_5", ["RLY-22"], async () => {
  const page = ORIGIN + B + "/d/bench-1/8080/";
  // rule 1
  eq(V.route(ORIGIN + B + "/d/bench-1/8080/api/ping?x=1", B, ORIGIN, null), { id: "bench-1", port: 8080, path: "/api/ping?x=1", rule: 1 }, "rule 1");
  eq(V.route(ORIGIN + B + "/d/bench-1/8080/", B, ORIGIN, null), { id: "bench-1", port: 8080, path: "/", rule: 1 }, "rule 1 root");
  eq(V.route(ORIGIN + B + "/d/bench-1/8080?q", B, ORIGIN, null), { id: "bench-1", port: 8080, redirect: B + "/d/bench-1/8080/?q" }, "missing slash");
  eq(V.route(ORIGIN + B + "/d/Bad_Id/8080/", B, ORIGIN, null), null, "bad id -> network");
  eq(V.route(ORIGIN + B + "/d/bench-1/70000/", B, ORIGIN, null), null, "bad port -> network");
  eq(V.route(ORIGIN + B + "/d/bench-1/0/", B, ORIGIN, null), null, "port 0 -> network");
  // rule 2: same origin, outside B, from a device page
  eq(V.route(ORIGIN + "/assets/js/main.js", B, ORIGIN, page), { id: "bench-1", port: 8080, path: "/assets/js/main.js", rule: 2 }, "rule 2");
  eq(V.route(ORIGIN + "/api/media/x.mp4?download=1", B, ORIGIN, ORIGIN + B + "/d/bench-1/9000/apps/cosmo/"),
     { id: "bench-1", port: 9000, path: "/api/media/x.mp4?download=1", rule: 2 }, "rule 2 from a sub page");
  // rule 3
  eq(V.route(ORIGIN + "/assets/x.js", B, ORIGIN, ORIGIN + B + "/"), null, "client is the device list -> network");
  eq(V.route(ORIGIN + "/assets/x.js", B, ORIGIN, null), null, "no client -> network");
  eq(V.route(ORIGIN + B + "/static/e2e.js", B, ORIGIN, page), null, "the device manager's own files -> network");
  eq(V.route(ORIGIN + B + "/api/devices", B, ORIGIN, page), null, "the device manager's API -> network");
  eq(V.route(ORIGIN + B, B, ORIGIN, page), null, "B itself -> network");
  eq(V.route("https://cdn.example.org/x.js", B, ORIGIN, page), null, "other origin -> network");
  eq(V.route(ORIGIN + "/x", B, ORIGIN, "https://evil.example.org" + B + "/d/bench-1/8080/"), null, "client of another origin");
  eq(V.route(ORIGIN + B + "x/y", B, ORIGIN, page), { id: "bench-1", port: 8080, path: B + "x/y", rule: 2 }, "a sibling of B is outside B");
});

test("viewer_location_rewrite", ["RLY-22"], async () => {
  const r = (v) => V.rewriteLocation(v, B, "bench-1", 8080, ORIGIN);
  eq(r("/login?next=%2F"), B + "/d/bench-1/8080/login?next=%2F", "absolute path");
  eq(r("http://127.0.0.1:8080/x#y"), B + "/d/bench-1/8080/x#y", "loopback with port");
  eq(r("http://localhost:8080/"), B + "/d/bench-1/8080/", "localhost");
  eq(r("http://[::1]:9000/a"), B + "/d/bench-1/9000/a", "another loopback port of the device");
  eq(r("http://127.0.0.1/"), B + "/d/bench-1/80/", "default port");
  eq(r("next/page"), "next/page", "relative stays");
  eq(r("?page=2"), "?page=2", "query only stays");
  eq(r("https://example.org/x"), "https://example.org/x", "other host stays");
  eq(r("//127.0.0.1:8080/p"), B + "/d/bench-1/8080/p", "protocol-relative loopback");
  eq(r("//example.org/p"), "//example.org/p", "protocol-relative other host");
  eq(r(B + "/d/bench-1/8080/x"), B + "/d/bench-1/8080/x", "already inside B");
  eq(r(ORIGIN + "/y"), B + "/d/bench-1/8080/y", "the relay's origin outside B");
});

test("viewer_cookie_jar", ["RLY-22"], async () => {
  const now = Date.UTC(2026, 0, 1);
  const p = (h, path = "/api/login") => V.parseSetCookie(h, path, now);
  const c = p("arstro_token_8080=abc%3D; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000");
  eq([c.name, c.value, c.path, c.expires, c.httpOnly, c.sameSite], ["arstro_token_8080", "abc%3D", "/", now + 31536000e3, true, "Strict"], "parse");
  eq(p("a=1").path, "/api", "default path is the request's directory");
  eq(p("a=1", "/").path, "/", "default path at the root");
  eq(p("a=1; Path=relative").path, "/api", "a bad Path falls back to the default");
  eq(p("a=1").expires, null, "session cookie");
  eq(p("a=1; Expires=Thu, 01 Jan 2099 00:00:00 GMT").expires, Date.UTC(2099, 0, 1), "Expires");
  eq(p("a=1; Expires=Thu, 01 Jan 1970 00:00:00 GMT").expires, 0, "Expires in the past = delete");
  eq(p("a=1; Max-Age=0; Expires=Thu, 01 Jan 2099 00:00:00 GMT").expires, 0, "Max-Age wins over Expires");
  eq(p("a=1; Max-Age=-5").expires, 0, "negative Max-Age = delete");
  eq(p("novalue"), null, "not a cookie");
  eq(p("=x"), null, "no name");
  eq(p('q="a b"; Path=/').value, '"a b"', "quoted value kept");

  let jar = V.applySetCookies([], ["arstro_token_8080=T1; Path=/; HttpOnly; Max-Age=100", "pref=dark; Path=/settings"], "/api/login", now);
  eq(jar.map((x) => x.name).sort(), ["arstro_token_8080", "pref"], "stored");
  eq(V.cookieHeader(jar, "/api/ping", now), "arstro_token_8080=T1", "path match /");
  eq(V.cookieHeader(jar, "/settings/x?y=1", now), "pref=dark; arstro_token_8080=T1", "longer path first");
  eq(V.cookieHeader(jar, "/settingsx", now), "arstro_token_8080=T1", "/settings does not match /settingsx");
  jar = V.applySetCookies(jar, ["arstro_token_8080=T2; Path=/; Max-Age=100"], "/", now + 1);
  eq(V.cookieHeader(jar, "/", now + 1), "arstro_token_8080=T2", "replaced, same name and path");
  eq(jar.length, 2, "no duplicate");
  // logout: Max-Age=0 deletes it
  jar = V.applySetCookies(jar, ["arstro_token_8080=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"], "/api/logout", now + 2);
  eq(V.cookieHeader(jar, "/", now + 2), "", "deleted by Max-Age=0");
  eq(jar.map((x) => x.name), ["pref"], "only the other cookie left");
  // expiry over time
  jar = V.applySetCookies(jar, ["short=1; Path=/; Max-Age=1"], "/", now);
  eq(V.cookieHeader(jar, "/", now + 500), "short=1", "alive");
  eq(V.cookieHeader(jar, "/", now + 1500), "", "expired");
  eq(V.applySetCookies(jar, [], "/", now + 1500).map((x) => x.name), ["pref"], "expired ones dropped");
  check(V.pathMatch("/a/b", "/a/") && V.pathMatch("/a", "/a") && !V.pathMatch("/ab", "/a"), "pathMatch");
});

test("viewer_html_injection", ["RLY-22"], async () => {
  const src = B + "/shim.js";
  const tag = `<script src="${src}"></script>`;
  eq(V.injectHtml("<!doctype html><html><head><title>x</title></head></html>", src),
     `<!doctype html><html><head>${tag}<title>x</title></head></html>`, "after <head>");
  eq(V.injectHtml('<html lang="en"><HEAD data-x="1">\n<meta charset="utf-8">', src),
     `<html lang="en"><HEAD data-x="1">${tag}\n<meta charset="utf-8">`, "head with attributes, any case");
  eq(V.injectHtml("<html><body><header>h</header></body></html>", src),
     `<html>${tag}<body><header>h</header></body></html>`, "<header> is not <head>; after <html>");
  eq(V.injectHtml("<!DOCTYPE html>\n<p>hi", src), `<!DOCTYPE html>${tag}\n<p>hi`, "after the doctype");
  eq(V.injectHtml("<p>hi", src), `${tag}<p>hi`, "at the start");
  const once = V.injectHtml("<head></head>", src);
  eq(V.injectHtml(once, src), once, "not twice");
  // bytes in another charset stay untouched
  const latin = Uint8Array.from([...enc.encode("<html><head><title>"), 0xe9, 0xff, ...enc.encode("</title></head>")]);
  const out = V.injectHtmlBytes(latin, src);
  eq(Buffer.from(out).toString("latin1"), Buffer.from(latin).toString("latin1").replace("<head>", "<head>" + tag), "bytes");
  eq(V.shimTag('/a"b&c/shim.js'), '<script src="/a&quot;b&amp;c/shim.js"></script>', "escaped src");
  check(V.isHtml("text/html; charset=utf-8") && V.isHtml("application/xhtml+xml") && !V.isHtml("text/plain") && !V.isHtml(""), "isHtml");
});

test("viewer_header_mapping", ["RLY-22"], async () => {
  const req = V.requestHeaders(new Map([["Accept", "*/*"], ["Range", "bytes=0-99"], ["Cookie", "x=1"], ["Host", "relay"],
                                        ["Connection", "keep-alive"], ["Content-Type", "application/json"],
                                        ["Service-Worker-Navigation-Preload", "true"], ["Proxy-Authorization", "x"]]));
  eq(req, [["accept", "*/*"], ["range", "bytes=0-99"], ["content-type", "application/json"]], "request headers");
  const res = V.responseHeaders([["Content-Type", "text/html"], ["Set-Cookie", "a=1"], ["set-cookie", "b=2; Path=/x"],
                                 ["Transfer-Encoding", "chunked"], ["Location", "/login"], ["Content-Range", "bytes 0-99/1000"],
                                 ["Clear-Site-Data", '"*"'], ["Content-Encoding", "GZIP"], ["X-Custom", "1"], ["bad"]],
                                B, "bench-1", 8080, ORIGIN);
  eq(res.setCookies, ["a=1", "b=2; Path=/x"], "set-cookie into the jar");
  eq(res.location, B + "/d/bench-1/8080/login", "location rewritten");
  eq(res.headers, [["content-type", "text/html"], ["location", B + "/d/bench-1/8080/login"], ["content-range", "bytes 0-99/1000"],
                   ["content-encoding", "GZIP"], ["x-custom", "1"]], "response headers");
  eq([res.contentType, res.encoding], ["text/html", "gzip"], "content type and encoding");
  check([101, 204, 205, 304].every((s) => V.NULL_BODY.has(s)) && !V.NULL_BODY.has(206), "null body statuses");
});

test("shim_context_and_url_rewrite", ["RLY-22"], async () => {
  const page = ORIGIN + B + "/d/bench-1/8080/#/apps";
  const ctx = S.shimContext(ORIGIN + B + "/shim.js", page);
  eq(ctx, { B, id: "bench-1", port: 8080, prefix: B + "/d/bench-1/8080", origin: ORIGIN, host: "relay.example.com" }, "context");
  eq(S.shimContext(ORIGIN + B + "/shim.js", ORIGIN + B + "/"), null, "not a device page");
  eq(S.shimContext("https://other.example" + B + "/shim.js", page), null, "shim from another origin");
  const rw = (u, base = page) => S.rewriteUrl(u, ctx, base);
  eq(rw("/apps/cosmo/"), B + "/d/bench-1/8080/apps/cosmo/", "absolute path (iframe)");
  eq(rw("/api/media/x.mp4?download=1#t=3"), B + "/d/bench-1/8080/api/media/x.mp4?download=1#t=3", "query and hash kept");
  eq(rw(ORIGIN + "/x"), B + "/d/bench-1/8080/x", "same-origin absolute URL");
  eq(rw("http://127.0.0.1:9000/"), B + "/d/bench-1/9000/", "device loopback, other port");
  eq(rw("apps/cosmo/"), null, "relative stays (it is inside B already)");
  eq(rw("#/files"), null, "hash link stays");
  eq(rw(B + "/"), null, "the device list stays");
  eq(rw("https://example.org/"), null, "other sites stay");
  eq(rw("blob:" + ORIGIN + "/1234"), null, "blob: stays");
  eq(rw("javascript:void 0"), null, "javascript: stays");
  // WebSocket targets
  const t = (u) => S.wsTarget(new URL(u), ctx);
  eq(t("wss://relay.example.com/ws"), { id: "bench-1", port: 8080, path: "/ws" }, "the app's /ws");
  eq(t("wss://relay.example.com/ws/preview?cam=1"), { id: "bench-1", port: 8080, path: "/ws/preview?cam=1" }, "with query");
  eq(t("ws://relay.example.com/ws"), { id: "bench-1", port: 8080, path: "/ws" }, "ws: on the same host");
  eq(t("wss://relay.example.com" + B + "/d/other-2/9000/sock"), { id: "other-2", port: 9000, path: "/sock" }, "explicit device URL");
  eq(t("wss://relay.example.com" + B + "/api/connect/bench-1"), null, "the device manager's own socket: native");
  eq(t("wss://echo.example.org/ws"), null, "another host: native");
});

// ------------------------------------------------------------------ static files sanity (RLY-22)
test("viewer_pages_have_no_inline_scripts", ["RLY-22", "RLY-24"], async () => {
  for (const f of fs.readdirSync(STATIC).filter((x) => x.endsWith(".html"))) {
    const html = fs.readFileSync(path.join(STATIC, f), "utf8");
    for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
      check(/\bsrc=/.test(m[1]) && !m[2].trim(), `${f}: inline script`);
      check(/src="\{\{BASE\}\}\//.test(m[1]), `${f}: scripts are loaded from {{BASE}}/ (the page is served at any depth)`);
    }
    check(!/\son[a-z]+\s*=/i.test(html.replace(/<script[\s\S]*?<\/script>/gi, "")), `${f}: inline event handler`);
    check(!/https?:\/\/(?!www\.w3\.org)/.test(html), `${f}: external URL`);
  }
  for (const f of ["e2e.js", "viewer.js", "shim.js", "sw.js", "app.js", "boot.js"]) {
    const js = fs.readFileSync(path.join(STATIC, f), "utf8");
    check(!/\beval\(|new Function\(/.test(js), `${f}: eval`);
    check(!/arstro\/internal\/devices/.test(js), `${f}: hard-coded base path`);
  }
});

// ------------------------------------------------------------------ runner
async function main() {
  if (args.includes("--list")) { for (const t of TESTS) console.log(t.name.padEnd(52), t.reqs.join(" ")); return 0; }
  let passed = 0, failed = 0;
  const covered = {};
  for (const t of TESTS.filter((x) => !ONLY || x.name.includes(ONLY))) {
    const t0 = Date.now();
    let ok = true;
    try {
      await t.fn();
    } catch (e) {
      ok = false;
      console.log(`FAIL  ${t.name.padEnd(50)} ${e.message}`);
    }
    if (ok) { passed++; console.log(`PASS  ${t.name.padEnd(50)} ${((Date.now() - t0) / 1000).toFixed(2).padStart(5)}s  ${t.reqs.join(" ")}`); }
    else failed++;
    for (const r of t.reqs) (covered[r] = covered[r] || []).push(ok);
  }
  console.log("\nrequirements: " + Object.keys(covered).sort().map((r) => r + (covered[r].every(Boolean) ? "" : "(FAIL)")).join(" "));
  console.log(`${passed} passed, ${failed} failed`);
  return failed ? 1 : 0;
}

process.exit(await main());
