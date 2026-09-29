// Live H.264 preview player (REC-02) for /ws/preview.
//
// Messages: text {"type":"config"|"state", ...}; binary [ver u8][flags u8][pts_us u64 BE][Annex-B AU].
// Decoding: WebCodecs when the page is a secure context (https or localhost) - lowest latency;
// otherwise Media Source Extensions through the vendored jmuxer (plain http on the LAN).

import { h, loadScript } from "./core.js";

const HEAD = 10;

export function previewSupport() {
  if ("VideoDecoder" in window) return "webcodecs";
  if (window.MediaSource || window.ManagedMediaSource) return "mse";
  return null;
}

function avcCodec(au) {
  for (let i = 0; i + 4 < au.length; i++) {
    if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1 && (au[i + 3] & 0x1f) === 7 && i + 6 < au.length) {
      const hex = (b) => b.toString(16).padStart(2, "0");
      return "avc1." + hex(au[i + 4]) + hex(au[i + 5]) + hex(au[i + 6]);
    }
  }
  return null;
}

export class PreviewPlayer {
  /** box: the .preview element. cb: {onState(state, detail), onConfig(cfg), onPlaying(bool)} */
  constructor(box, cb = {}) {
    this.box = box;
    this.cb = cb;
    this.mode = previewSupport();
    this.canvas = h("canvas");
    this.video = h("video", { muted: true, autoplay: true, playsInline: true, disablePictureInPicture: true });
    this.video.muted = true;
    this.video.setAttribute("playsinline", "");
    box.prepend(this.canvas, this.video);
    this.ws = null;
    this.wanted = false;
    this.playing = false;
    this.config = null;
    this.stats = { frames: 0, bytes: 0, since: performance.now() };
    this.video.addEventListener("playing", () => this.setPlaying(true));
  }

  start() {
    this.wanted = true;
    if (!this.mode) {
      this.cb.onState?.("unsupported", "This browser can't decode the live stream. Use a current Chrome, Edge, Firefox or Safari, or the app.");
      return;
    }
    if (!this.ws) this.connect();
  }

  stop() {
    this.wanted = false;
    clearTimeout(this.retry);
    if (this.ws) { const ws = this.ws; this.ws = null; ws.close(); }
    this.teardown();
  }

  destroy() {
    this.stop();
    this.canvas.remove();
    this.video.remove();
  }

  connect() {
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/preview";
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.needKey = true;
    ws.onmessage = (e) => {
      if (typeof e.data === "string") this.onText(JSON.parse(e.data));
      else this.onFrame(new Uint8Array(e.data));
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.teardown();
      if (this.wanted) {
        this.cb.onState?.("reconnecting", "");
        this.retry = setTimeout(() => this.wanted && this.connect(), 1500);
      }
    };
  }

  onText(msg) {
    if (msg.type === "config") {
      const changed = !this.config || this.config.width !== msg.width || this.config.height !== msg.height;
      this.config = msg;
      if (changed) this.teardown(true);
      this.cb.onConfig?.(msg);
    } else if (msg.type === "state") {
      if (msg.state !== "live") this.setPlaying(false);
      this.cb.onState?.(msg.state, msg.detail || "");
    }
  }

  onFrame(buf) {
    if (buf.length <= HEAD || buf[0] !== 1) return;
    const key = (buf[1] & 1) === 1;
    const pts = Number(new DataView(buf.buffer, buf.byteOffset + 2, 8).getBigUint64(0));
    const au = buf.subarray(HEAD);
    this.stats.frames++;
    this.stats.bytes += au.length;
    if (this.needKey && !key) return;
    if (this.mode === "webcodecs") this.decodeWebCodecs(key, pts, au);
    else this.decodeMse(key, pts, au);
  }

  // ----------------------------------------------------------- WebCodecs
  decodeWebCodecs(key, pts, au) {
    if (!this.decoder) {
      if (!key) return;
      const codec = avcCodec(au);
      if (!codec) return;
      const ctx = this.canvas.getContext("2d");
      try {
        this.decoder = new VideoDecoder({
          output: (frame) => {
            if (this.canvas.width !== frame.displayWidth) this.canvas.width = frame.displayWidth;
            if (this.canvas.height !== frame.displayHeight) this.canvas.height = frame.displayHeight;
            ctx.drawImage(frame, 0, 0);
            frame.close();
            this.canvas.classList.add("on");
            this.setPlaying(true);
          },
          error: (e) => { console.warn("decoder", e); this.resetDecoder(); },
        });
        this.decoder.configure({ codec, optimizeForLatency: true });
      } catch (e) {
        console.warn("WebCodecs unavailable, using MSE", e);
        this.decoder = null;
        this.mode = "mse";
        return this.decodeMse(key, pts, au);
      }
    }
    if (this.decoder.decodeQueueSize > 2) {   // falling behind: skip to the next keyframe (stay live)
      this.needKey = true;
      if (!key) return;
    }
    try {
      this.decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: pts, data: au }));
      this.needKey = false;
    } catch (e) {
      this.resetDecoder();
    }
  }

  resetDecoder() {
    try { this.decoder && this.decoder.state !== "closed" && this.decoder.close(); } catch (e) { /* closed */ }
    this.decoder = null;
    this.needKey = true;
  }

  // ----------------------------------------------------------------- MSE
  async decodeMse(key, pts, au) {
    if (!this.jmuxer) {
      if (!key) return;
      if (!window.JMuxer) {
        if (!this.loading) this.loading = loadScript("/assets/vendor/jmuxer.min.js").catch(() => null);
        await this.loading;
        if (!window.JMuxer) return;
        if (!this.ws) return;
      }
      if (this.jmuxer) return;
      this.jmuxer = new window.JMuxer({
        node: this.video, mode: "video", flushingTime: 0, maxDelay: 250, clearBuffer: true, live: true,
        fps: (this.config && this.config.fps) || 30, debug: false,
        onError: () => { this.teardown(true); },
      });
      this.video.classList.add("on");
      this.lastPts = null;
      // MSE buffers; keep the picture at the live edge (smooth and current beats complete)
      clearInterval(this.chase);
      this.chase = setInterval(() => {
        const v = this.video;
        if (!v.buffered.length) return;
        const end = v.buffered.end(v.buffered.length - 1);
        const behind = end - v.currentTime;
        if (behind > 0.3) v.currentTime = end - 0.03;
        else v.playbackRate = behind > 0.12 ? 1.08 : 1;
        if (v.paused) v.play().catch(() => {});
      }, 200);
    }
    const fps = (this.config && this.config.fps) || 30;
    let dur = 1000 / fps;
    if (this.lastPts !== null && pts > this.lastPts) dur = Math.min(200, Math.max(1, (pts - this.lastPts) / 1000));
    this.lastPts = pts;
    this.jmuxer.feed({ video: au, duration: dur });
    this.needKey = false;
    if (this.video.paused) this.video.play().catch(() => {});
  }

  teardown(keepSocket = false) {
    this.resetDecoder();
    clearInterval(this.chase);
    if (this.jmuxer) { try { this.jmuxer.destroy(); } catch (e) { /* gone */ } this.jmuxer = null; }
    this.video.removeAttribute("src");
    this.canvas.classList.remove("on");
    this.video.classList.remove("on");
    this.needKey = true;
    if (!keepSocket) this.setPlaying(false);
    else this.setPlaying(false);
  }

  setPlaying(on) {
    if (this.playing === on) return;
    this.playing = on;
    this.box.classList.toggle("playing", on);
    this.cb.onPlaying?.(on);
  }

  /** Measured incoming rate since the last call: {fps, kbps}. */
  sample() {
    const now = performance.now();
    const dt = (now - this.stats.since) / 1000 || 1;
    const out = { fps: this.stats.frames / dt, kbps: this.stats.bytes * 8 / 1000 / dt };
    this.stats = { frames: 0, bytes: 0, since: now };
    return out;
  }
}
