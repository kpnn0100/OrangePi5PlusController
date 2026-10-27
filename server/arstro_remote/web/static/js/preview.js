// Live H.264 preview player (REC-02) for /ws/preview.
//
// Messages: text {"type":"config"|"state", ...}; binary [ver u8][flags u8][pts_us u64 BE][Annex-B AU].
// Decoding: WebCodecs when the page is a secure context (https or localhost) - lowest latency;
// otherwise Media Source Extensions through the vendored jmuxer (plain http on the LAN).
// Film work (REC-10, REC-13, REC-14): after setWant([...]) the player says `hello` on every
// connection; then text `levels` / `zoom` and binary exposure data arrive too. Binaries are
// routed by the first byte: 1 = the picture, 2 = exposure data, 3 = the focus loupe's own stream
// (decoded by a second lane into `loupe.canvas` / `loupe.video`, which the loupe shows).
// send() talks back (the loupe).

import { h, loadScript } from "./core.js";

const HEAD = 10;

export function previewSupport() {
  if ("VideoDecoder" in window) return "webcodecs";
  if (window.MediaSource || window.ManagedMediaSource) return "mse";
  return null;
}

/** The SPS NAL of an access unit (its start offset and end), or null. */
function spsAt(au) {
  for (let i = 0; i + 4 < au.length; i++) {
    if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1 && (au[i + 3] & 0x1f) === 7) {
      let j = i + 4;
      while (j + 2 < au.length && !(au[j] === 0 && au[j + 1] === 0 && (au[j + 2] === 1 || au[j + 2] === 0))) j++;
      return [i + 3, j + 2 < au.length ? j : au.length];
    }
  }
  return null;
}

function avcCodec(au) {
  const s = spsAt(au);
  if (!s || s[0] + 3 >= au.length) return null;
  const hex = (b) => b.toString(16).padStart(2, "0");
  return "avc1." + hex(au[s[0] + 1]) + hex(au[s[0] + 2]) + hex(au[s[0] + 3]);
}

/**
 * One H.264 stream decoded into its own canvas (WebCodecs) or video element (MSE, jmuxer):
 * the main picture, or the loupe. show(bool) says whether it shows a moving picture.
 */
class Lane {
  constructor(player, { canvas, video, show, fps, name, watchSps = false }) {
    this.p = player;
    this.canvas = canvas;
    this.video = video;
    this.show = show;
    this.fps = fps;                   // () => the stream's frame rate (MSE durations)
    this.name = name;
    this.watchSps = watchSps;         // a new SPS (a new encoder, maybe a new size) starts afresh
    this.mode = player.mode;
    this.decoder = null;
    this.jmuxer = null;
    this.needKey = true;
    this.mseOpen = false;
    this.lastPts = null;
    this.sps = null;
    this.frames = 0;                  // access units fed, pictures shown (WebCodecs) - for tests
    this.shown = 0;
  }

  feed(key, pts, au) {
    if (key && this.watchSps) {
      const s = spsAt(au);
      const sps = s ? Array.prototype.join.call(au.subarray(s[0], s[1]), ",") : null;
      if (sps && this.sps && sps !== this.sps && (this.decoder || this.jmuxer)) {
        this.p.note(this.name + " new sps");
        this.reset();
      }
      if (sps) this.sps = sps;
    }
    if (this.needKey && !key) return;
    this.frames++;
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
            this.shown++;
            this.canvas.classList.add("on");
            this.show(true);
          },
          error: (e) => { console.warn(this.name + " decoder", e); this.resetDecoder(); },
        });
        this.decoder.configure({ codec, optimizeForLatency: true });
      } catch (e) {
        console.warn("WebCodecs unavailable, using MSE", e);
        this.decoder = null;
        this.mode = this.p.mode = "mse";          // for every lane from now on
        this.p.loadMse();
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
  decodeMse(key, pts, au) {
    // Synchronous on purpose: frames must reach the muxer in order, starting with a
    // keyframe. jmuxer also needs its MediaSource *open* before the first frame (else it
    // builds the init segment into nothing and never recovers): create it, wait for
    // onReady, and start at the next keyframe (every 0.5 s).
    if (!this.jmuxer) {
      if (!key) return;
      if (!window.JMuxer) {
        this.p.loadMse();
        this.needKey = true;
        return;
      }
      this.p.note(this.name + " mse start");
      this.jmuxer = new window.JMuxer({
        node: this.video, mode: "video", flushingTime: 0, maxDelay: 250, clearBuffer: true, live: true,
        fps: this.fps() || 30, debug: false,
        onReady: () => { this.mseOpen = true; this.p.note(this.name + " mse open"); },
        onError: (e) => {
          this.p.note(this.name + " jmuxer error " + (e && (e.message || e.type || e)));
          this.reset();
          this.show(false);
        },
      });
      this.mseOpen = false;
      this.video.classList.add("on");
      this.lastPts = null;
      // MSE buffers; keep the picture at the live edge (smooth and current beats complete)
      clearInterval(this.chase);
      this.chase = setInterval(() => {
        const v = this.video;
        if (!v.buffered.length) return;
        const end = v.buffered.end(v.buffered.length - 1);
        if (v.readyState < 2) {                         // not started yet: start at the live edge
          if (end > 0.2 && v.currentTime < end - 0.3) v.currentTime = end - 0.05;
          if (v.paused) v.play().catch(() => {});
          return;
        }
        const behind = end - v.currentTime;
        if (behind > 0.3) v.currentTime = end - 0.03;
        else v.playbackRate = behind > 0.12 ? 1.08 : 1;
        if (v.paused) v.play().catch(() => {});
        else if (v.currentTime > 0) this.show(true);   // frames are moving
      }, 200);
    }
    if (!this.mseOpen) {               // not open yet: wait for the next keyframe
      this.needKey = true;
      return;
    }
    if (this.needKey && !key) return;
    const fps = this.fps() || 30;
    let dur = 1000 / fps;
    if (this.lastPts !== null && pts > this.lastPts) dur = Math.min(200, Math.max(1, (pts - this.lastPts) / 1000));
    this.lastPts = pts;
    this.jmuxer.feed({ video: au, duration: dur });
    this.needKey = false;
    if (this.video.paused) this.video.play().catch(() => {});
  }

  /** Forget the stream: the next keyframe starts a new decoder. */
  reset() {
    this.resetDecoder();
    clearInterval(this.chase);
    this.mseOpen = false;
    if (this.jmuxer) { try { this.jmuxer.destroy(); } catch (e) { /* gone */ } this.jmuxer = null; }
    this.video.removeAttribute("src");
    this.canvas.classList.remove("on");
    this.video.classList.remove("on");
    this.needKey = true;
    this.sps = null;
    this.mode = this.p.mode;
  }
}

function videoEl() {
  const v = h("video", { muted: true, autoplay: true, playsInline: true, disablePictureInPicture: true });
  v.muted = true;
  v.setAttribute("playsinline", "");
  return v;
}

export class PreviewPlayer {
  /** box: the .preview element. cb: {onState(state, detail), onConfig(cfg), onPlaying(bool), onOpen(), onClose(),
   *  onLevels(msg), onAnalysis(buf), onZoom(msg), onLoupe(showing)} */
  constructor(box, cb = {}, path = "/ws/preview") {
    this.box = box;
    this.cb = cb;
    this.path = path;                 // /ws/preview (HDMI) or /ws/screen (the Pi desktop)
    this.mode = previewSupport();
    this.canvas = h("canvas");
    this.video = videoEl();
    box.prepend(this.canvas, this.video);
    this.ws = null;
    this.wanted = false;
    this.playing = false;
    this.config = null;
    this.stats = { frames: 0, bytes: 0, since: performance.now() };
    this.want = null;                 // what this viewer asks for in `hello` (null: says nothing)
    this.main = new Lane(this, { canvas: this.canvas, video: this.video, name: "main",
                                 show: (on) => this.setPlaying(on), fps: () => this.config && this.config.fps });
    // the focus loupe (REC-14): its own stream, decoded into elements the loupe shows
    this.zoom = null;                 // the server's last `zoom` while the loupe is on
    this.loupeShowing = false;
    this.loupe = new Lane(this, { canvas: h("canvas"), video: videoEl(), name: "loupe", watchSps: true,
                                  show: (on) => this.setLoupeShowing(on), fps: () => this.zoom && this.zoom.fps });
    this.video.addEventListener("playing", () => this.setPlaying(true));
    this.events = [];                 // last events, for bug reports (window.arstroPreview)
    window.arstroPreview = this;
  }

  note(what) {
    this.events.push(`${(performance.now() / 1000).toFixed(2)} ${what}`);
    if (this.events.length > 40) this.events.shift();
  }

  start() {
    this.wanted = true;
    if (this.mode === "mse") this.loadMse();
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
    this.endLoupe();
  }

  destroy() {
    this.stop();
    this.canvas.remove();
    this.video.remove();
    this.loupe.canvas.remove();
    this.loupe.video.remove();
  }

  connect() {
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + this.path;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.main.needKey = true;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.sendHello();
      this.cb.onOpen?.();
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        let msg;
        // a silent channel may come as -Infinity dB, which JSON does not allow
        try { msg = JSON.parse(e.data); } catch (x) { msg = JSON.parse(e.data.replace(/-Infinity/g, "-999")); }
        this.onText(msg);
      } else this.onFrame(new Uint8Array(e.data));
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.teardown();
      this.endLoupe();                // a new connection is a new viewer: no loupe
      this.cb.onClose?.();
      if (this.wanted) {
        this.cb.onState?.("reconnecting", "");
        this.retry = setTimeout(() => this.wanted && this.connect(), 1500);
      }
    };
  }

  /** Ask for meters / exposure data (REC-10, REC-13): sent now and on every reconnect. */
  setWant(list) {
    const key = list.join(",");
    if (this.want && this.want.join(",") === key) return;
    this.want = list;
    this.sendHello();
  }

  sendHello() {
    if (this.want) this.send({ type: "hello", want: this.want });
  }

  /** A text message to the server (hello, zoom); false when not connected. */
  send(obj) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(obj));
    return true;
  }

  onText(msg) {
    if (msg.type === "levels") return this.cb.onLevels?.(msg);
    this.note(msg.type + " " + (msg.state || (msg.type === "zoom" ? (msg.on ? `on ${msg.width}x${msg.height}` : "off " + (msg.error || ""))
                                                                  : `${msg.width}x${msg.height}`)));
    if (msg.type === "zoom") {
      if (msg.on) {
        // a new size (or ratio) is a new loupe stream, starting with its keyframe
        const z = this.zoom;
        if (!z || z.width !== msg.width || z.height !== msg.height) this.resetLoupe();
        this.zoom = msg;
      } else this.endLoupe();
      return this.cb.onZoom?.(msg);
    }
    if (msg.type === "config") {
      const changed = !this.config || this.config.width !== msg.width || this.config.height !== msg.height;
      this.config = msg;
      if (changed) this.teardown();
      this.cb.onConfig?.(msg);
    } else if (msg.type === "state") {
      if (msg.state !== "live") this.setPlaying(false);
      this.cb.onState?.(msg.state, msg.detail || "");
    }
  }

  onFrame(buf) {
    const ver = buf[0];
    if (ver === 2) return this.cb.onAnalysis?.(buf);          // exposure data (REC-13)
    if (buf.length <= HEAD || (ver !== 1 && ver !== 3)) return;
    const key = (buf[1] & 1) === 1;
    const pts = Number(new DataView(buf.buffer, buf.byteOffset + 2, 8).getBigUint64(0));
    const au = buf.subarray(HEAD);
    if (ver === 3) {                                          // the loupe (REC-14)
      if (this.zoom) this.loupe.feed(key, pts, au);
      return;
    }
    this.stats.frames++;
    this.stats.bytes += au.length;
    this.main.feed(key, pts, au);
  }

  loadMse() {
    if (!this.loading) this.loading = loadScript("/assets/vendor/jmuxer.min.js").catch(() => null);
    return this.loading;
  }

  /** The main picture starts afresh at its next keyframe. */
  teardown() {
    this.main.reset();
    this.setPlaying(false);
  }

  resetLoupe() {
    this.loupe.reset();
    this.setLoupeShowing(false);
  }

  /** The loupe is off (or this viewer is gone): no loupe stream any more. */
  endLoupe() {
    this.zoom = null;
    this.resetLoupe();
  }

  setPlaying(on) {
    if (this.playing === on) return;
    this.note("playing " + on);
    this.playing = on;
    this.box.classList.toggle("playing", on);
    this.cb.onPlaying?.(on);
  }

  setLoupeShowing(on) {
    if (this.loupeShowing === on) return;
    this.note("loupe showing " + on);
    this.loupeShowing = on;
    this.cb.onLoupe?.(on);
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
