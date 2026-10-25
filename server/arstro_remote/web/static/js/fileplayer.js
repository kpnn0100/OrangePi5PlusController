// Recording player for any browser (GAL-11): the server streams a fragmented MP4
// (/api/play/<file>?mode=copy|h264&t=S) and this plays it with Media Source Extensions.
//   copy - the video as it is (H.265 stays H.265: full quality, no load on the Pi), when this
//          browser decodes it (MediaSource.isTypeSupported with the file's exact codec string)
//   h264 - otherwise the Pi's VPU converts it to H.264 (15 fps at 4K, one viewer at a time)
// Seeking starts a new stream at that time; the bar shows the file's time (offset + currentTime).

import { h, icon, fmtDur } from "./core.js";

const MS = window.ManagedMediaSource || window.MediaSource;
const H264_MIME = "avc1.640033";          // what the Pi's H.264 conversion produces (High@5.1)
const AHEAD = 30;                          // s buffered before we stop reading (backpressure)

function mime(video, audio) {
  return `video/mp4; codecs="${video}${audio ? ",mp4a.40.2" : ""}"`;
}

/** {mode, mime} this browser can play the item with, or null. */
export function streamMode(item) {
  if (!MS || !item || item.recording || item.problem || !["hevc", "h264"].includes(item.codec)) return null;
  const ok = (m) => { try { return MS.isTypeSupported(m); } catch (e) { return false; } };
  if (item.mse && ok(mime(item.mse, item.audio))) return { mode: "copy", mime: mime(item.mse, item.audio) };
  if (item.codec === "hevc" && ok(mime(H264_MIME, item.audio))) return { mode: "h264", mime: mime(H264_MIME, item.audio) };
  return null;
}

export class FilePlayer {
  constructor(root, item, { poster } = {}) {
    this.item = item;
    this.how = streamMode(item);
    this.offset = 0;
    this.gen = 0;
    this.duration = item.duration || 0;
    this.video = h("video", { playsInline: true, poster: poster || "", "data-id": item.id });
    this.video.disableRemotePlayback = true;             // ManagedMediaSource (Safari) wants it
    this.playBtn = h("button.btn.ghost.icon.sm", { "aria-label": "Play", onclick: () => this.toggle() }, icon("play"));
    this.seek = h("input.seek", { type: "range", min: "0", max: String(Math.max(1, this.duration)), step: "0.1", value: "0",
                                  "aria-label": "Position" });
    this.time = h("span.mono.dim", null, "");
    this.badge = h("span.tag", { class: this.how?.mode === "copy" ? "accent" : "warn",
                                 title: this.how?.mode === "copy" ? "the original video, no conversion"
                                   : "this browser has no H.265 decoder: the Pi converts it (15 fps)" },
                   this.how?.mode === "copy" ? (item.codec === "hevc" ? "H.265 original" : "Original") : "H.264 · 15 fps");
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
    this.renderTime(0);
    if (!this.how) this.fail("This browser can't play this video (no Media Source support).");
    else this.load(0, false);
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
      if (gen === this.gen && e.name !== "AbortError") this.fail("The stream stopped: " + e.message);
    }
  }

  destroy() {
    this.gen++;
    this.abort?.abort();
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    if (this.url) URL.revokeObjectURL(this.url);
  }
}
