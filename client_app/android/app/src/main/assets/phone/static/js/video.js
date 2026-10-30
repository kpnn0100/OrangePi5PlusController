// Live H.264 from the phone (PHONE.md *Video*): /ws/screen and /ws/camera.
//
// Phone → viewer: text {"t": "config", "codec": "avc1.PPCCLL", width, height, rotation} (again on
// every format change), then binary  flags u8 (bit0 = key frame) ‖ pts_us u64 BE ‖ Annex-B access
// unit (SPS/PPS in front of every key frame). Decoded with WebCodecs into a canvas (the page is
// always a secure context behind the device manager, so VideoDecoder exists in current browsers).
// Viewer → phone: text JSON ({"t": "keyframe"}, {"t": "quality", max}, control messages).

import { h, wsUrl } from "./core.js";

const HEAD = 9;

export function videoSupported() {
  return "VideoDecoder" in window && "EncodedVideoChunk" in window;
}

/** The codec string from the SPS of an access unit ("avc1.42c01f"), or null. */
function avcFromSps(au) {
  for (let i = 0; i + 6 < au.length; i++) {
    if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1 && (au[i + 3] & 0x1f) === 7) {
      const hex = (b) => b.toString(16).padStart(2, "0");
      return "avc1." + hex(au[i + 4]) + hex(au[i + 5]) + hex(au[i + 6]);
    }
  }
  return null;
}

export class VideoView {
  /**
   * box: an element (position: relative) the canvas goes into.
   * opts: {rotate: apply config.rotation to the picture (camera), onState(state, detail), onConfig(cfg),
   *        onMessage(msg) for other text messages, onOpen()}
   * States: connecting, waiting (connected, no picture yet), live, reconnecting, unsupported, stopped.
   */
  constructor(box, opts = {}) {
    this.box = box;
    this.opts = opts;
    this.canvas = h("canvas.video");
    box.prepend(this.canvas);
    this.ctx = this.canvas.getContext("2d");
    this.ws = null;
    this.path = null;
    this.wanted = false;
    this.decoder = null;
    this.codec = null;
    this.needKey = true;
    this.config = null;
    this.state = "stopped";
    this.received = 0;               // access units received / pictures shown (tests, stats)
    this.shown = 0;
    this.bytes = 0;
    this.lastKeyAsk = 0;
    this.extra = null;               // a message sent on every (re)connect, e.g. the quality
    window.phoneDebug = window.phoneDebug || {};
    (window.phoneDebug.videos = window.phoneDebug.videos || []).push(this);
  }

  setState(s, detail = "") {
    if (this.state === s && this.detail === detail) return;
    this.state = s;
    this.detail = detail;
    this.box.classList.toggle("playing", s === "live");
    this.opts.onState?.(s, detail);
  }

  /** Open (or re-open with another path, e.g. a new camera). */
  start(path) {
    if (!videoSupported()) {
      this.setState("unsupported", "This browser can't decode the phone's video. Use a current Chrome, Edge, Safari or Firefox.");
      return;
    }
    if (path && path !== this.path) { this.path = path; this.close(); }
    this.wanted = true;
    if (!this.ws) this.connect();
  }

  stop() {
    this.wanted = false;
    clearTimeout(this.retry);
    this.close();
    this.setState("stopped");
  }

  destroy() {
    this.stop();
    this.canvas.remove();
    const list = window.phoneDebug?.videos;
    if (list) { const i = list.indexOf(this); if (i >= 0) list.splice(i, 1); }
  }

  close() {
    if (this.ws) { const ws = this.ws; this.ws = null; try { ws.close(); } catch (e) { /* gone */ } }
    this.resetDecoder();
    this.canvas.classList.remove("on");
    this.config = null;
  }

  connect() {
    clearTimeout(this.retry);
    this.setState(this.state === "reconnecting" ? "reconnecting" : "connecting");
    let ws;
    try { ws = new WebSocket(wsUrl(this.path)); } catch (e) { this.again(""); return; }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.needKey = true;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.setState("waiting");
      if (this.extra) this.send(this.extra);
      this.send({ t: "keyframe" });
      this.opts.onOpen?.();
    };
    ws.onmessage = (e) => {
      if (this.ws !== ws) return;
      if (typeof e.data === "string") {
        let msg;
        try { msg = JSON.parse(e.data); } catch (x) { return; }
        if (msg.t === "config") this.onConfig(msg);
        else this.opts.onMessage?.(msg);
      } else this.onFrame(new Uint8Array(e.data));
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.resetDecoder();
      this.again(e.reason || "");
    };
  }

  again(reason) {
    if (!this.wanted) return;
    this.setState("reconnecting", reason);
    this.retry = setTimeout(() => this.wanted && !this.ws && this.connect(), reason ? 3000 : 1500);
  }

  /** A text message to the phone; false when not connected. */
  send(obj) {
    if (!this.ws || this.ws.readyState !== 1) return false;
    this.ws.send(JSON.stringify(obj));
    return true;
  }

  /** Sent now and after every reconnect (quality). */
  setExtra(obj) {
    this.extra = obj;
    this.send(obj);
  }

  askKeyframe() {
    const now = performance.now();
    if (now - this.lastKeyAsk < 1000) return;
    this.lastKeyAsk = now;
    this.send({ t: "keyframe" });
  }

  onConfig(msg) {
    const old = this.config;
    this.config = msg;
    if (!old || old.width !== msg.width || old.height !== msg.height || old.codec !== msg.codec) this.resetDecoder();
    this.opts.onConfig?.(msg);
  }

  onFrame(buf) {
    if (buf.length <= HEAD) return;
    const key = (buf[0] & 1) === 1;
    const pts = Number(new DataView(buf.buffer, buf.byteOffset + 1, 8).getBigUint64(0));
    const au = buf.subarray(HEAD);
    this.received++;
    this.bytes += au.length;
    if (this.needKey && !key) { this.askKeyframe(); return; }
    if (!this.decoder) {
      if (!key) return;
      const codec = (this.config && /^avc1\.[0-9a-fA-F]{6}$/.test(this.config.codec || "") && this.config.codec) || avcFromSps(au);
      if (!codec) return;
      try {
        this.decoder = new VideoDecoder({ output: (f) => this.draw(f), error: (e) => { console.warn("video decoder", e); this.resetDecoder(); this.askKeyframe(); } });
        this.decoder.configure({ codec, optimizeForLatency: true });
        this.codec = codec;
      } catch (e) {
        console.warn("video decoder", e);
        this.decoder = null;
        this.setState("unsupported", "This browser can't decode the phone's video (" + codec + ").");
        this.wanted = false;
        this.close();
        return;
      }
    }
    if (this.decoder.decodeQueueSize > 3) {          // falling behind: skip to the next key frame (stay live)
      this.needKey = true;
      this.askKeyframe();
      if (!key) return;
    }
    try {
      this.decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: pts, data: au }));
      this.needKey = false;
    } catch (e) {
      this.resetDecoder();
      this.askKeyframe();
    }
  }

  draw(frame) {
    const rot = this.opts.rotate && this.config ? (((Number(this.config.rotation) || 0) % 360) + 360) % 360 : 0;
    const fw = frame.displayWidth, fh = frame.displayHeight;
    const side = rot === 90 || rot === 270;
    const cw = side ? fh : fw, ch = side ? fw : fh;
    if (this.canvas.width !== cw) this.canvas.width = cw;
    if (this.canvas.height !== ch) this.canvas.height = ch;
    const c = this.ctx;
    if (rot) {
      c.save();
      c.translate(cw / 2, ch / 2);
      c.rotate((rot * Math.PI) / 180);
      c.drawImage(frame, -fw / 2, -fh / 2, fw, fh);
      c.restore();
    } else c.drawImage(frame, 0, 0, fw, fh);
    frame.close();
    this.shown++;
    if (!this.canvas.classList.contains("on")) this.canvas.classList.add("on");
    if (this.state !== "live") this.setState("live");
  }

  resetDecoder() {
    try { if (this.decoder && this.decoder.state !== "closed") this.decoder.close(); } catch (e) { /* closed */ }
    this.decoder = null;
    this.needKey = true;
  }

  /** The picture's rectangle on screen (the canvas is letterboxed: object-fit contain), or null. */
  pictureRect() {
    const w = this.canvas.width, hgt = this.canvas.height;
    if (!w || !hgt || !this.shown) return null;
    const r = this.canvas.getBoundingClientRect();
    const k = Math.min(r.width / w, r.height / hgt);
    const pw = w * k, ph = hgt * k;
    return { left: r.left + (r.width - pw) / 2, top: r.top + (r.height - ph) / 2, width: pw, height: ph };
  }

  /** A client point → [x, y] in 0..1 of the picture, or null outside it. */
  toPicture(clientX, clientY, clamp = false) {
    const p = this.pictureRect();
    if (!p) return null;
    let x = (clientX - p.left) / p.width, y = (clientY - p.top) / p.height;
    if (clamp) { x = Math.min(1, Math.max(0, x)); y = Math.min(1, Math.max(0, y)); }
    else if (x < 0 || y < 0 || x > 1 || y > 1) return null;
    return [Math.round(x * 10000) / 10000, Math.round(y * 10000) / 10000];
  }

  /** Measured since the last call: {fps, kbps}. */
  sample() {
    const now = performance.now();
    const dt = (now - (this.since || now)) / 1000 || 1;
    const out = { fps: (this.received - (this.lastRecv || 0)) / dt, kbps: ((this.bytes - (this.lastBytes || 0)) * 8) / 1000 / dt };
    this.since = now;
    this.lastRecv = this.received;
    this.lastBytes = this.bytes;
    return out;
  }
}
