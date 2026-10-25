// Recording player for any browser (GAL-11): the server streams a fragmented MP4
// (/api/play/<file>?mode=copy|h264&t=S) and this plays it with Media Source Extensions.
//   copy - the video as it is (H.265 stays H.265: full quality, no load on the Pi), when this
//          device decodes it
//   h264 - otherwise the Pi's VPU converts it to H.264 (15 fps at 4K, one viewer at a time)
// "Decodes it" is asked with MediaCapabilities (codec AND size, frame rate, bitrate - a phone
// whose decoder stops at 1080p says no to 4K, where isTypeSupported only checks the codec),
// and proven by playing: a decode error moves on to the next mode at the same position and
// is remembered on this device. When nothing works, onCantPlay() lets the page offer a
// smaller H.264 copy (the Pi has no leak-free way to scale 4K live).
// Seeking starts a new stream at that time; the bar shows the file's time (offset + currentTime).

import { h, icon, fmtDur } from "./core.js";

const MS = window.ManagedMediaSource || window.MediaSource;
const H264_MIME = "avc1.640033";          // what the Pi's H.264 conversion produces (High@5.1)
const AHEAD = 30;                          // s buffered before we stop reading (backpressure)
const H264 = { fps: 15, bps: 12e6 };        // the Pi's conversion (recorder/play.py)
const NO_KEY = "arstro.play.cant.";        // + codec@WxH: this device failed to decode it

function mime(video, audio) {
  return `video/mp4; codecs="${video}${audio ? ",mp4a.40.2" : ""}"`;
}

function remembered(codec, item) {
  try { return localStorage.getItem(NO_KEY + `${codec}@${item.width}x${item.height}`) === "1"; } catch (e) { return false; }
}

function remember(codec, item) {
  try { localStorage.setItem(NO_KEY + `${codec}@${item.width}x${item.height}`, "1"); } catch (e) { /* private mode */ }
}

/** The modes worth trying, best first: [{mode, mime, codec, fps, bps}]. Synchronous (codec only). */
export function candidates(item) {
  if (!MS || !item || item.recording || item.problem || !["hevc", "h264"].includes(item.codec)) return [];
  const ok = (m) => { try { return MS.isTypeSupported(m); } catch (e) { return false; } };
  const out = [];
  const bps = item.duration ? Math.round(item.size * 8 / item.duration) : 20e6;
  if (item.mse && ok(mime(item.mse, item.audio)) && !remembered(item.mse, item))
    out.push({ mode: "copy", codec: item.mse, mime: mime(item.mse, item.audio), fps: item.fps || 30, bps });
  if (item.codec === "hevc" && ok(mime(H264_MIME, item.audio)) && !remembered(H264_MIME, item))
    out.push({ mode: "h264", codec: H264_MIME, mime: mime(H264_MIME, item.audio), fps: H264.fps, bps: H264.bps });
  return out;
}

/** Can this browser try to stream the item at all? (the gallery's play button) */
export function streamMode(item) {
  return candidates(item)[0] || null;
}

/** Does the device decode this mode at the item's size? (MediaCapabilities; true when unknown) */
async function decodes(c, item) {
  if (!navigator.mediaCapabilities?.decodingInfo || !item.width) return true;
  try {
    const r = await navigator.mediaCapabilities.decodingInfo({ type: "media-source", video: {
      contentType: `video/mp4; codecs="${c.codec}"`, width: item.width, height: item.height,
      bitrate: Math.max(1, Math.round(c.bps)), framerate: c.fps || 30 } });
    return r.supported;
  } catch (e) {
    return true;
  }
}

export class FilePlayer {
  constructor(root, item, { poster, onCantPlay } = {}) {
    this.item = item;
    this.onCantPlay = onCantPlay;
    this.todo = candidates(item);                        // modes still to try, best first
    this.how = null;
    this.offset = 0;
    this.gen = 0;
    this.duration = item.duration || 0;
    this.video = h("video", { playsInline: true, poster: poster || "", "data-id": item.id });
    this.video.disableRemotePlayback = true;             // ManagedMediaSource (Safari) wants it
    this.playBtn = h("button.btn.ghost.icon.sm", { "aria-label": "Play", onclick: () => this.toggle() }, icon("play"));
    this.seek = h("input.seek", { type: "range", min: "0", max: String(Math.max(1, this.duration)), step: "0.1", value: "0",
                                  "aria-label": "Position" });
    this.time = h("span.mono.dim", null, "");
    this.badge = h("span.tag.hidden");
    this.msg = h("div.note.hidden");
    const fs = h("button.btn.ghost.icon.sm", { "aria-label": "Full screen",
      onclick: () => (document.fullscreenElement ? document.exitFullscreen() : root.requestFullscreen?.()) }, icon("expand"));
    this.bar = h("div.fp-bar", null, this.playBtn, this.time, this.seek, this.badge, fs);
    root.append(this.video, this.msg, this.bar);
    root.classList.add("fp");
    let dragging = false;
    this.seek.addEventListener("input", () => { dragging = true; this.renderTime(Number(this.seek.value)); });
    this.seek.addEventListener("change", () => { dragging = false; this.load(Number(this.seek.value), !this.video.paused || this.wantPlay); });
    this.video.addEventListener("click", () => this.toggle());
    this.video.addEventListener("timeupdate", () => { if (!dragging) this.renderTime(this.now()); });
    this.video.addEventListener("play", () => this.renderBtn());
    this.video.addEventListener("pause", () => this.renderBtn());
    this.video.addEventListener("ended", () => this.renderBtn());
    this.video.addEventListener("error", () => this.decodeFailed());
    this.renderTime(0);
    this.next(0, false);
  }

  /** Take the next mode this device decodes and start it at t. */
  async next(t, play) {
    const gen = ++this.gen;
    while (this.todo.length) {
      const c = this.todo.shift();
      if (!(await decodes(c, this.item))) continue;
      if (gen !== this.gen) return;
      this.how = c;
      const copy = c.mode === "copy";
      this.badge.className = "tag " + (copy ? "accent" : "warn");
      this.badge.textContent = copy ? (this.item.codec === "hevc" ? "H.265 original" : "Original") : "H.264 · 15 fps";
      this.badge.title = copy ? "the original video, no conversion"
        : "this device can't decode the original: the Pi converts it to H.264 (15 fps)";
      return this.load(t, play || this.wantPlay);                // play pressed while choosing
    }
    this.how = null;
    const big = this.item.width > 1920;
    this.fail(big ? `This device can't decode ${this.item.width}×${this.item.height} video. A 1080p H.264 copy plays here.`
                  : "This device can't decode this video.");
    this.onCantPlay?.(big);
  }

  /** The device failed on the stream it said it could decode: remember, try the next mode here. */
  decodeFailed() {
    if (!this.how || this.failing || this.destroyed) return;
    this.failing = true;
    const err = this.video.error;
    console.warn("playback failed", this.how.mode, err?.code, err?.message);
    remember(this.how.codec, this.item);
    const t = this.now(), play = this.wantPlay || !this.video.paused;
    this.how = null;
    setTimeout(() => { this.failing = false; this.next(t, play); }, 0);
  }

  now() { return this.offset + (this.video.currentTime || 0); }

  renderTime(t) {
    this.seek.value = String(t);
    this.time.textContent = `${fmtDur(t)} / ${fmtDur(this.duration)}`;
  }

  renderBtn() {
    const playing = !this.video.paused && !this.video.ended;
    this.playBtn.replaceChildren(icon(playing ? "pause" : "play"));
    this.playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
  }

  toggle() {
    if (!this.how) { this.wantPlay = !this.wantPlay; return; }    // still choosing: play once ready
    if (this.video.ended) return this.load(0, true);
    if (this.video.paused) { this.wantPlay = true; this.video.play().catch(() => {}); }
    else { this.wantPlay = false; this.video.pause(); }
  }

  fail(text) {
    this.msg.textContent = text;
    this.msg.classList.remove("hidden");
  }

  /** (Re)start the stream at t seconds of the file. */
  load(t, play) {
    if (!this.how) return;
    const gen = ++this.gen;
    this.abort?.abort();
    this.abort = new AbortController();
    this.msg.classList.add("hidden");
    this.offset = Math.max(0, Math.min(t, Math.max(0, this.duration - 0.5)));
    this.wantPlay = play;
    const ms = new MS();
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = URL.createObjectURL(ms);
    this.video.src = this.url;
    ms.addEventListener("sourceopen", () => this.pump(ms, gen, this.abort.signal), { once: true });
    this.renderTime(this.offset);
  }

  async pump(ms, gen, signal) {
    let sb;
    try {
      sb = ms.addSourceBuffer(this.how.mime);
    } catch (e) {
      return this.fail("Can't play: " + e.message);
    }
    const done = () => new Promise((r) => sb.addEventListener("updateend", r, { once: true }));
    const url = `/api/play/${encodeURIComponent(this.item.id)}?mode=${this.how.mode}&t=${this.offset.toFixed(2)}`;
    let resp;
    try {
      resp = await fetch(url, { signal, credentials: "same-origin" });
    } catch (e) {
      if (gen === this.gen) this.fail("The stream stopped: " + e.message);
      return;
    }
    if (gen !== this.gen) return;
    if (!resp.ok) {
      let why = resp.statusText;
      try { why = (await resp.json()).error || why; } catch (e) { /* not JSON */ }
      return this.fail("Can't play: " + why);
    }
    const reader = resp.body.getReader();
    let started = false;
    try {
      for (;;) {
        const { value, done: eof } = await reader.read();
        if (gen !== this.gen) return;
        if (eof) break;
        // keep ~AHEAD s buffered: the rest waits on the server (TCP backpressure)
        while (sb.buffered.length && sb.buffered.end(sb.buffered.length - 1) - this.video.currentTime > AHEAD) {
          await new Promise((r) => setTimeout(r, 300));
          if (gen !== this.gen) return;
        }
        for (;;) {
          try { sb.appendBuffer(value); await done(); break; }
          catch (e) {
            if (e.name !== "QuotaExceededError") throw e;
            const cut = this.video.currentTime - 5;              // full: drop what was watched
            if (cut <= 0) { await new Promise((r) => setTimeout(r, 300)); continue; }
            sb.remove(0, cut); await done();
          }
        }
        if (!started && sb.buffered.length) {
          started = true;
          if (this.wantPlay) this.video.play().catch(() => {});
        }
      }
      if (gen === this.gen && ms.readyState === "open") ms.endOfStream();
    } catch (e) {
      if (gen !== this.gen || e.name === "AbortError") return;
      if (this.video.error) return this.decodeFailed();           // the decoder gave up
      this.fail("The stream stopped: " + e.message);
    }
  }

  destroy() {
    this.destroyed = true;
    this.gen++;
    this.abort?.abort();
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    if (this.url) URL.revokeObjectURL(this.url);
  }
}
