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
