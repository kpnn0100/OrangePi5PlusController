// On-set monitoring in the live view: audio meters (REC-10), recording tally, frame guides,
// safe areas, centre cross, zebras, false colour, histogram / waveform (REC-13) and the focus
// loupe (REC-14).
//
// Drawn here from what /ws/preview sends once the player said `hello` (levels, exposure data)
// and from the monitoring settings every viewer shares. The loupe is this viewer's own: view
// state, never saved - a circle over the whole picture showing the loupe stream the server
// cuts from the full-resolution frame. Overlays and the loupe follow the picture's displayed
// rect (object-fit: contain, so letterboxing never shifts them).

import { h, clear, icon, toast } from "./core.js";

export const GUIDES = [["off", "Off"], ["2.39", "2.39:1"], ["1.85", "1.85:1"], ["16:9", "16:9"], ["4:3", "4:3"],
                       ["1:1", "1:1"], ["9:16", "9:16"]];
const GUIDE_RATIO = { "2.39": 2.39, "1.85": 1.85, "16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1, "9:16": 9 / 16 };
export const ZOOMS = [2, 3, 4, 6, 8];
const DB_MIN = -60;                     // meter scale, dBFS
const CLIP_DB = -1;                     // a peak at or over this shows red (REC-10)
const SCALE = [-48, -24, -12, -6, 0];
const HOLD_MS = 1200, FALL_DB_S = 20;   // peak hold, then the bars fall this fast (meter ballistics)
const dec = new TextDecoder();

/** The picture's rect inside a box of bw x bh showing sw x sh with object-fit: contain. */
export function fitRect(bw, bh, sw, sh) {
  if (!sw || !sh || !bw || !bh) return { x: 0, y: 0, w: bw, h: bh };
  const k = Math.min(bw / sw, bh / sh);
  const w = sw * k, hh = sh * k;
  return { x: (bw - w) / 2, y: (bh - hh) / 2, w, h: hh };
}

/** Exposure data: [2][1][json len u16 BE][JSON][hist 128][wave wl x ww][map gw x gh] -> parts. */
export function parseAnalysis(buf) {
  if (buf.length < 4 || buf[0] !== 2 || buf[1] !== 1) return null;
  const n = (buf[2] << 8) | buf[3];
  let meta;
  try { meta = JSON.parse(dec.decode(buf.subarray(4, 4 + n))); } catch (e) { return null; }
  let o = 4 + n;
  const hist = buf.subarray(o, o + 128); o += 128;
  const wave = buf.subarray(o, o + meta.ww * meta.wl); o += meta.ww * meta.wl;
  const map = buf.subarray(o, o + meta.gw * meta.gh);
  if (map.length < meta.gw * meta.gh) return null;
  return { meta, hist, wave, map, at: performance.now() };
}

const LOUPE = { share: 0.39, min: 140, max: 320 };   // the circle: 39 % of the picture's height, in CSS px
const SEND_MS = 66;                                    // moves sent at most ~15 a second
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const r4 = (v) => Math.round(v * 10000) / 10000;

/**
 * Where the loupe goes in the picture's displayed rect r: its diameter d, its centre (cx, cy)
 * and the point it magnifies (px, py). A finger would hide a circle under it: on touch the
 * circle floats above the point (beside it when there is no room above).
 */
export function loupeGeometry(r, fx, fy, touch) {
  const d = Math.round(Math.min(clamp(r.h * LOUPE.share, LOUPE.min, LOUPE.max), r.w, r.h));
  const px = r.x + fx * r.w, py = r.y + fy * r.h;
  let cx = px, cy = py;
  if (touch) {
    const off = d / 2 + Math.max(0.1 * d, 22);
    if (py - off - d / 2 >= r.y) cy = py - off;
    else cx = r.x + r.w - (px + off + d / 2) >= (px - off - d / 2) - r.x ? px + off : px - off;
  }
  cx = clamp(cx, r.x + d / 2, r.x + r.w - d / 2);
  cy = clamp(cy, r.y + d / 2, r.y + r.h - d / 2);
  return { d, cx, cy, px, py };
}

/** A band table's colour per map value (IRE x 2), RGBA, for false colour. */
function bandLut(bands) {
  const lut = new Uint8ClampedArray(256 * 4);
  for (let v = 0; v < 256; v++) {
    const ire = v / 2;
    const b = bands.find((x) => ire >= x.from && ire < x.to);
    if (!b) continue;
    const c = /^#?([0-9a-f]{6})$/i.exec(b.colour || "");
    if (!c) continue;
    const n = parseInt(c[1], 16);
    lut.set([n >> 16, (n >> 8) & 255, n & 255, 190], v * 4);
  }
  return lut;
}

/** A tile of diagonal black / white stripes: zebras show on white and on colours alike. */
function stripes() {
  const t = document.createElement("canvas");
  t.width = t.height = 12;
  const c = t.getContext("2d");
  c.lineWidth = 3;
  for (const [col, d] of [["rgba(255,255,255,.95)", 0], ["rgba(0,0,0,.85)", 6]]) {
    c.strokeStyle = col;
    for (const o of [-12, 0, 12]) { c.beginPath(); c.moveTo(o + d, 12); c.lineTo(o + d + 12, 0); c.stroke(); }
  }
  return t;
}

export class OnSet {
  /** box: the .preview element; player: its PreviewPlayer. */
  constructor(box, player) {
    this.box = box;
    this.player = player;
    this.mon = {};                       // the monitoring settings
    this.rec = {};                       // the recorder status
    this.data = null;                    // the last exposure data
    this.lp = null;                      // the loupe while open: {fx, fy} the point, 0..1 of the picture
    this.touch = false;                  // placed by a finger: the circle floats above the point
    this.sent = null;                    // the last zoom message sent
    this.sentAt = 0;
    this.offs = 0;                       // "off"s sent and not answered yet
    this.size = 0;                       // the circle's diameter / the picture's displayed height
    this.tick = 0;
    this.lut = bandLut([]);
    this.bandsKey = "";

    this.canvas = h("canvas.mon-ov", { "aria-hidden": "true" });
    this.lutTag = h("span.glass.lut-tag.hidden");

    // the loupe (REC-14): a circle showing the player's loupe lane, a mark on the point it
    // magnifies when the circle is not right over it (a finger, an edge), a line between them
    this.wait = h("span.loupe-wait", { "aria-hidden": "true" }, "…");
    this.glass = h("div.loupe-glass", { title: "Drag to move the loupe" },
                   player.loupe.canvas, player.loupe.video, this.wait);
    this.tag = h("span.loupe-tag", null, "3×");
    this.closeBtn = h("button.loupe-x", { type: "button", title: "Close the loupe (Esc)", "aria-label": "Close the loupe",
                                          onclick: (e) => { e.stopPropagation(); this.closeLoupe(); } }, icon("x"));
    this.loupeEl = h("div.loupe", { role: "group", "aria-label": "Focus loupe" }, this.glass, this.tag, this.closeBtn);
    this.stem = h("i.loupe-stem");
    this.mark = h("i.loupe-mark", { title: "Drag to move the loupe" });
    this.layer = h("div.loupe-layer.hidden", null, this.stem, this.mark, this.loupeEl);
    box.append(this.canvas, this.layer);
    box.setAttribute("title", "Click the picture for a focus loupe");

    // the strip under the picture: meters and the scope
    this.metersTitle = h("span.ellipsis");
    this.meterRows = h("div.meter-rows", null, h("div.muted.meter-wait", null, "Waiting for audio…"));
    this.meters = h("div.meters.hidden", null, h("div.strip-title", null, icon("volume"), this.metersTitle), this.meterRows);
    this.meterEls = [];
    this.scope = h("canvas.scope", { "aria-label": "Scope" });
    this.scopeTitle = h("span");
    this.scopeBox = h("div.scope-box.hidden", null, h("div.strip-title", null, icon("signal"), this.scopeTitle), this.scope);
    this.strip = h("div.mon-strip.hidden", null, this.meters, this.scopeBox);

    this.onClick = (e) => this.click(e);
    this.onDown = (e) => { this.lastPointer = e.pointerType; };
    this.onKey = (e) => {
      // Esc closes the loupe - unless a sheet is open (Esc closes that first); capture phase,
      // so this runs before core.js closes it
      if (e.key !== "Escape" || !this.lp) return;
      if (document.querySelector(".sheet.show")) return;
      this.closeLoupe();
    };
    box.addEventListener("click", this.onClick);
    box.addEventListener("pointerdown", this.onDown, true);
    window.addEventListener("keydown", this.onKey, true);
    for (const el of [this.glass, this.mark]) {          // drag the loupe (mouse, finger, pen)
      el.addEventListener("pointerdown", (e) => this.grab(e));
      el.addEventListener("pointermove", (e) => this.dragTo(e));
      el.addEventListener("pointerup", (e) => this.release(e));
      el.addEventListener("pointercancel", (e) => this.release(e));
      el.addEventListener("lostpointercapture", (e) => this.release(e));
    }
    this.ro = new ResizeObserver(() => { this.draw(); this.drawScope(); this.place(); });
    this.ro.observe(box);
    this.ro.observe(this.scope);
    this.stale = setInterval(() => this.checkStale(), 1000);
  }

  destroy() {
    this.box.removeEventListener("click", this.onClick);
    this.box.removeEventListener("pointerdown", this.onDown, true);
    window.removeEventListener("keydown", this.onKey, true);
    this.ro.disconnect();
    clearInterval(this.stale);
    clearTimeout(this.sendTimer);
    clearTimeout(this.sizeTimer);
  }

  // ------------------------------------------------------------- inputs
  /** The shared monitoring settings changed. */
  settings(m) {
    this.mon = m || {};
    const want = [];
    if (this.mon.meters) want.push("levels");
    if (this.mon.zebra || this.mon.false_color || (this.mon.scope && this.mon.scope !== "off")) want.push("analysis");
    this.player.setWant(want);
    this.renderStrip();
    this.renderTags();
    this.renderLoupe();
    this.draw();
    this.drawScope();
  }

  /** The recorder status changed: tally, bands, LUT, audio source. */
  status(rec) {
    this.rec = rec || {};
    const r = this.rec.recording || {};
    this.box.classList.toggle("tally", !!r.active);
    this.box.classList.toggle("tally-end", !!(r.active && r.stopping));
    const bands = (this.rec.monitor || {}).bands || [];
    const key = JSON.stringify(bands);
    if (key !== this.bandsKey) { this.bandsKey = key; this.lut = bandLut(bands); this.draw(); }
    this.renderTags();
    this.renderStrip();
  }

  /** The socket (re)opened or closed: a fresh viewer has no loupe. */
  reset() {
    this.offs = 0;
    this.hideLoupe();
    this.data = null;
    this.draw();
  }

  levels(msg) {
    if (!this.mon.meters) return;
    const peak = msg.peak || [], rms = msg.rms || [];
    this.lastLevels = performance.now();
    this.meters.classList.remove("stale");
    if (this.meterEls.length !== peak.length) this.buildMeters(peak.length);
    const pct = (db) => Math.max(0, Math.min(100, ((Number(db) - DB_MIN) / -DB_MIN) * 100));
    const now = performance.now();
    peak.forEach((p, i) => {
      const m = this.meterEls[i];
      p = Number(p);
      const r = Number(rms[i] ?? p);
      const dt = Math.min(1, (now - (m.at || now)) / 1000);
      m.at = now;
      // the peak holds a moment, then falls; RMS falls smoothly - short sounds stay readable
      if (p >= m.hold || now - m.holdAt > HOLD_MS) {
        if (p >= m.hold) { m.hold = p; m.holdAt = now; } else m.hold = Math.max(p, m.hold - FALL_DB_S * dt);
      }
      m.rms = r >= m.rms ? r : Math.max(r, m.rms - FALL_DB_S * 1.5 * dt);
      m.rest.style.width = (100 - pct(m.rms)) + "%";
      m.pk.style.left = `calc(${pct(m.hold)}% - 1px)`;
      m.pk.style.opacity = m.hold <= DB_MIN ? "0" : "1";
      m.db.textContent = m.hold <= -99 ? "-∞" : m.hold.toFixed(0);
      if (p >= CLIP_DB) m.clipUntil = now + 1500;
      m.row.classList.toggle("clip", now < m.clipUntil);
    });
  }

  analysis(buf) {
    const a = parseAnalysis(buf);
    if (!a) return;
    this.data = a;
    this.tick++;
    this.draw();
    this.drawScope();
  }

  /** The server's `zoom`: the loupe's stream geometry, or the loupe ended / refused. */
  onZoom(z) {
    if (!z.on) {
      if (z.error) toast("Focus loupe: " + z.error, "err");
      else if (this.offs > 0) {          // the answer to a close: already closed, or open again since
        this.offs--;
        return;
      }
      this.hideLoupe();
      return;
    }
    if (!this.lp) return;                // closed meanwhile: its "off" is on its way
    this.renderLoupe();
  }

  /** The loupe lane shows a picture (true) or waits for its first keyframe (false). */
  loupeShowing(on) {
    this.wait.classList.toggle("hidden", !!on);
  }

  // -------------------------------------------------------------- loupe
  /** The source size of the picture shown. */
  srcSize() {
    const p = this.player;
    if (p.canvas.classList.contains("on") && p.canvas.width) return [p.canvas.width, p.canvas.height];
    if (p.video.classList.contains("on") && p.video.videoWidth) return [p.video.videoWidth, p.video.videoHeight];
    if (p.config && p.config.width) return [p.config.width, p.config.height];
    const s = this.rec.signal || {};
    return [s.width || 16, s.height || 9];
  }

  rect() {
    const [sw, sh] = this.srcSize();
    return fitRect(this.box.clientWidth, this.box.clientHeight, sw, sh);
  }

  /** A click (tap) on the picture opens the loupe there, or moves it there. */
  click(e) {
    if (e.target.closest("button, a, input, select, .ov, .loupe-layer")) return;
    if (!this.player.playing) return;
    const b = this.box.getBoundingClientRect();
    const r = this.rect();
    const fx = (e.clientX - b.left - this.box.clientLeft - r.x) / r.w;
    const fy = (e.clientY - b.top - this.box.clientTop - r.y) / r.h;
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return;          // on the black bars
    this.touch = this.lastPointer === "touch";
    const opening = !this.lp;
    this.lp = { fx, fy };
    if (opening) {
      this.sent = null;
      this.player.resetLoupe();
      this.loupeShowing(false);
    }
    this.renderLoupe();
    if (!this.sendZoom() && opening) this.hideLoupe();          // not connected
  }

  grab(e) {
    if (!this.lp || e.button > 0 || this.drag) return;
    e.preventDefault();
    e.stopPropagation();
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (x) { /* gone */ }
    this.touch = e.pointerType === "touch";
    const r = this.rect();
    this.drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY,
                  px0: r.x + this.lp.fx * r.w, py0: r.y + this.lp.fy * r.h };
    this.layer.classList.add("dragging");
    this.place();
  }

  dragTo(e) {
    const g = this.drag;
    if (!g || e.pointerId !== g.id || !this.lp) return;
    const r = this.rect();
    this.lp = { fx: clamp((g.px0 + e.clientX - g.x0 - r.x) / r.w, 0, 1),
                fy: clamp((g.py0 + e.clientY - g.y0 - r.y) / r.h, 0, 1) };
    this.place();
    const wait = SEND_MS - (performance.now() - this.sentAt);
    if (wait <= 0) this.sendZoom();
    else if (!this.sendTimer) this.sendTimer = setTimeout(() => { this.sendTimer = 0; this.sendZoom(); }, wait);
  }

  release(e) {
    if (!this.drag || e.pointerId !== this.drag.id) return;
    this.drag = null;
    this.layer.classList.remove("dragging");
    this.sendZoom();                     // where it was let go
  }

  /** Tell the server where the loupe is and how big; false when not connected. */
  sendZoom() {
    clearTimeout(this.sendTimer);
    this.sendTimer = 0;
    clearTimeout(this.sizeTimer);
    if (!this.lp) return false;
    const msg = { type: "zoom", on: true, x: r4(clamp(this.lp.fx, 0, 1)), y: r4(clamp(this.lp.fy, 0, 1)), size: r4(this.size) };
    const last = this.sent;
    if (last && last.x === msg.x && last.y === msg.y && last.size === msg.size) return true;
    if (!this.player.send(msg)) return false;
    this.sent = msg;
    this.sentAt = performance.now();
    return true;
  }

  closeLoupe() {
    if (!this.lp) return;
    if (this.player.send({ type: "zoom", on: false })) this.offs++;
    this.player.endLoupe();
    this.hideLoupe();
  }

  hideLoupe() {
    this.lp = null;
    this.drag = null;
    this.sent = null;
    clearTimeout(this.sendTimer);
    this.sendTimer = 0;
    clearTimeout(this.sizeTimer);
    this.renderLoupe();
  }

  renderLoupe() {
    const on = !!this.lp;
    this.layer.classList.toggle("hidden", !on);
    this.box.classList.toggle("loupe-on", on);
    if (!on) return;
    const z = this.player.zoom;
    this.tag.textContent = ((z && z.ratio) || Number(this.mon.zoom) || 3) + "×";
    this.wait.classList.toggle("hidden", this.player.loupeShowing);
    this.place();
  }

  /** Put the circle (and the mark, and the line between them) where the loupe is. */
  place() {
    if (!this.lp) return;
    const r = this.rect();
    if (!r.w || !r.h) return;
    const g = loupeGeometry(r, this.lp.fx, this.lp.fy, this.touch);
    const { d, cx, cy, px, py } = g;
    Object.assign(this.loupeEl.style, { width: d + "px", height: d + "px",
                                        transform: `translate3d(${cx - d / 2}px, ${cy - d / 2}px, 0)` });
    const dx = px - cx, dy = py - cy, dist = Math.hypot(dx, dy);
    const off = dist > 3;
    this.mark.classList.toggle("hidden", !off);
    this.stem.classList.toggle("hidden", dist - d / 2 < 6);
    if (off) {
      this.mark.style.transform = `translate3d(${px}px, ${py}px, 0)`;
      const len = Math.max(0, dist - d / 2 - 4);
      Object.assign(this.stem.style, { width: len + "px",
        transform: `translate3d(${px}px, ${py}px, 0) rotate(${Math.atan2(-dy, -dx)}rad) translateX(4px)` });
    }
    // past 2 device pixels per pixel of the loupe stream: pixels, not a blur (focus checking)
    const z = this.player.zoom;
    this.glass.classList.toggle("px", !!(z && z.width && (d * (window.devicePixelRatio || 1)) / z.width >= 2));
    // a new circle size (window resized, full screen): the server cuts a new crop
    this.size = d / r.h;
    if (this.sent && Math.abs(r4(this.size) - this.sent.size) > 0.004 && !this.drag) {
      clearTimeout(this.sizeTimer);
      this.sizeTimer = setTimeout(() => this.sendZoom(), 250);
    }
  }

  renderTags() {
    const lut = this.mon.lut;
    const on = lut && lut !== "off";
    this.lutTag.classList.toggle("hidden", !on);
    if (on) {
      const l = ((this.rec.monitor || {}).luts || []).find((x) => x.id === lut);
      this.lutTag.textContent = "LUT · " + (l ? l.name.split(" →")[0] : lut.replace(/^cube:/, ""));
      this.lutTag.title = "The preview is shown through this LUT; the recording is not";
    }
  }

  // ------------------------------------------------------------ overlay
  draw() {
    const cv = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const W = Math.round(this.box.clientWidth * dpr), H = Math.round(this.box.clientHeight * dpr);
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    const m = this.mon;
    if (!this.player.playing) return;
    const r0 = this.rect();
    const r = { x: r0.x * dpr, y: r0.y * dpr, w: r0.w * dpr, h: r0.h * dpr };
    const d = this.data && performance.now() - this.data.at < 3000 ? this.data : null;
    if (d && m.false_color) this.drawFalseColour(ctx, r, d);
    if (d && m.zebra) this.drawZebras(ctx, r, d);
    // the delivered frame: a guide, or the whole picture
    let f = r;
    const ratio = GUIDE_RATIO[m.guides];
    if (ratio) {
      let gw = r.w, gh = r.h;
      if (ratio > r.w / r.h) gh = r.w / ratio; else gw = r.h * ratio;
      f = { x: r.x + (r.w - gw) / 2, y: r.y + (r.h - gh) / 2, w: gw, h: gh };
      ctx.fillStyle = "rgba(0,0,0,.5)";
      ctx.fillRect(r.x, r.y, r.w, f.y - r.y);
      ctx.fillRect(r.x, f.y + f.h, r.w, r.y + r.h - f.y - f.h);
      ctx.fillRect(r.x, f.y, f.x - r.x, f.h);
      ctx.fillRect(f.x + f.w, f.y, r.x + r.w - f.x - f.w, f.h);
      ctx.strokeStyle = "rgba(255,255,255,.85)";
      ctx.lineWidth = Math.max(1, dpr);
      ctx.strokeRect(f.x + .5, f.y + .5, f.w - 1, f.h - 1);
      ctx.fillStyle = "rgba(255,255,255,.85)";
      ctx.font = `${Math.round(10.5 * dpr)}px ui-monospace, monospace`;
      ctx.textBaseline = "top";
      ctx.fillText(GUIDES.find(([k]) => k === m.guides)[1], f.x + 6 * dpr, f.y + 5 * dpr);
    }
    if (m.safe) {
      ctx.lineWidth = Math.max(1, dpr);
      for (const [k, dash] of [[0.93, []], [0.9, [6 * dpr, 4 * dpr]]]) {      // action, title
        ctx.setLineDash(dash);
        ctx.strokeStyle = k === 0.93 ? "rgba(255,255,255,.55)" : "rgba(255,255,255,.75)";
        ctx.strokeRect(f.x + f.w * (1 - k) / 2, f.y + f.h * (1 - k) / 2, f.w * k, f.h * k);
      }
      ctx.setLineDash([]);
    }
    if (m.center) {
      const cx = f.x + f.w / 2, cy = f.y + f.h / 2, a = Math.max(8, Math.min(f.w, f.h) * 0.04);
      ctx.strokeStyle = "rgba(0,0,0,.5)";
      ctx.lineWidth = 3 * dpr;
      ctx.beginPath(); ctx.moveTo(cx - a, cy); ctx.lineTo(cx + a, cy); ctx.moveTo(cx, cy - a); ctx.lineTo(cx, cy + a); ctx.stroke();
      ctx.strokeStyle = "rgba(255,255,255,.9)";
      ctx.lineWidth = Math.max(1, dpr);
      ctx.stroke();
    }
  }

  drawFalseColour(ctx, r, d) {
    const { gw, gh } = d.meta;
    const img = new ImageData(gw, gh);
    const px = img.data, lut = this.lut;
    for (let i = 0; i < gw * gh; i++) {
      const o = d.map[i] * 4;
      if (lut[o + 3]) { px[i * 4] = lut[o]; px[i * 4 + 1] = lut[o + 1]; px[i * 4 + 2] = lut[o + 2]; px[i * 4 + 3] = lut[o + 3]; }
    }
    const t = this.cellCanvas(gw, gh);
    t.getContext("2d").putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(t, r.x, r.y, r.w, r.h);
  }

  drawZebras(ctx, r, d) {
    const { gw, gh } = d.meta;
    const lvl = Math.round((this.mon.zebra_level || 95) * 2);
    const img = new ImageData(gw, gh);
    const px = img.data;
    let any = false;
    for (let i = 0; i < gw * gh; i++) if (d.map[i] >= lvl) { px[i * 4 + 3] = 255; any = true; }
    if (!any) return;
    const mask = this.cellCanvas(gw, gh);
    mask.getContext("2d").putImageData(img, 0, 0);
    const w = Math.max(1, Math.round(r.w)), hh = Math.max(1, Math.round(r.h));
    const z = this.zebraCanvas || (this.zebraCanvas = document.createElement("canvas"));
    if (z.width !== w || z.height !== hh) { z.width = w; z.height = hh; }
    const c = z.getContext("2d");
    c.globalCompositeOperation = "copy";
    const pat = c.createPattern(this.stripeTile || (this.stripeTile = stripes()), "repeat");
    pat.setTransform(new DOMMatrix().translate((this.tick % 4) * 3, 0));   // the stripes crawl
    c.fillStyle = pat;
    c.fillRect(0, 0, w, hh);
    c.globalCompositeOperation = "destination-in";
    c.imageSmoothingEnabled = false;
    c.drawImage(mask, 0, 0, w, hh);
    c.globalCompositeOperation = "source-over";
    ctx.drawImage(z, r.x, r.y);
  }

  cellCanvas(gw, gh) {
    const t = this.cells || (this.cells = document.createElement("canvas"));
    if (t.width !== gw || t.height !== gh) { t.width = gw; t.height = gh; }
    return t;
  }

  // -------------------------------------------------------------- strip
  renderStrip() {
    const m = this.mon;
    const scope = m.scope && m.scope !== "off" ? m.scope : null;
    this.meters.classList.toggle("hidden", !m.meters);
    this.scopeBox.classList.toggle("hidden", !scope);
    this.strip.classList.toggle("hidden", !m.meters && !scope);
    this.scopeTitle.textContent = scope === "waveform" ? "Waveform · luma" : "Histogram · luma";
    const src = (this.rec.monitor || {}).audio_source;
    const inp = (this.rec.audio_inputs || []).find((i) => i.id === src);
    this.metersTitle.textContent = "Audio" + (inp ? " · " + inp.name : src ? " · " + src : "");
  }

  buildMeters(n) {
    this.meterEls = [];
    const names = n === 2 ? ["L", "R"] : Array.from({ length: n }, (_, i) => String(i + 1));
    const rows = names.map((name) => {
      const rest = h("i.rest"), pk = h("i.pk"), db = h("span.db.num");
      const row = h("div.meter", null, h("span.ch", null, name), h("div.mbar", null, rest, pk), db);
      this.meterEls.push({ row, rest, pk, db, clipUntil: 0, hold: -999, holdAt: 0, rms: -999, at: 0 });
      return row;
    });
    const scale = h("div.meter.scale", null, h("span.ch"),
      h("div.mscale", null, SCALE.map((v) => h("span", { style: { left: ((v - DB_MIN) / -DB_MIN) * 100 + "%" } }, String(v)))),
      h("span.db", null, "dBFS"));
    clear(this.meterRows, n ? rows : h("div.muted.meter-wait", null, "No audio channels"), n ? scale : null);
  }

  checkStale() {
    if (this.mon.meters && this.lastLevels && performance.now() - this.lastLevels > 2000) this.meters.classList.add("stale");
  }

  drawScope() {
    const kind = this.mon.scope;
    if (!kind || kind === "off") return;
    const cv = this.scope;
    const dpr = window.devicePixelRatio || 1;
    const W = Math.round(cv.clientWidth * dpr), H = Math.round(cv.clientHeight * dpr);
    if (!W || !H) return;
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    const d = this.data && performance.now() - this.data.at < 3000 ? this.data : null;
    const max = (d && d.meta.ire_max) || 110;
    const padL = 24 * dpr, padB = kind === "histogram" ? 14 * dpr : 0;
    const pw = W - padL, ph = H - padB;
    if (d && kind === "waveform") {
      const { ww, wl } = d.meta;
      const img = new ImageData(ww, wl);
      for (let i = 0; i < wl; i++) {                    // row 0 = 0 %: the bottom
        for (let c = 0; c < ww; c++) {
          const v = d.wave[i * ww + c];
          if (!v) continue;
          const o = ((wl - 1 - i) * ww + c) * 4;
          img.data[o] = 150; img.data[o + 1] = 255; img.data[o + 2] = 170; img.data[o + 3] = Math.min(255, v * 1.4);
        }
      }
      const t = this.waveCanvas || (this.waveCanvas = document.createElement("canvas"));
      if (t.width !== ww || t.height !== wl) { t.width = ww; t.height = wl; }
      t.getContext("2d").putImageData(img, 0, 0);
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(t, padL, 0, pw, ph);
    } else if (d) {
      ctx.fillStyle = "rgba(236,238,241,.7)";
      ctx.beginPath();
      ctx.moveTo(padL, ph);
      for (let b = 0; b < Math.min(128, max); b++) {
        const x0 = padL + (b / max) * pw, x1 = padL + ((b + 1) / max) * pw, y = ph - (d.hist[b] / 255) * ph;
        ctx.lineTo(x0, y); ctx.lineTo(x1, y);
      }
      ctx.lineTo(padL + pw, ph);
      ctx.closePath();
      ctx.fill();
    }
    // graticule: 0, 50, 100 %
    ctx.font = `${Math.round(9.5 * dpr)}px ui-monospace, monospace`;
    ctx.lineWidth = 1;
    for (const ire of [0, 50, 100]) {
      ctx.strokeStyle = ire === 100 ? "rgba(255,107,107,.55)" : "rgba(255,255,255,.22)";
      ctx.fillStyle = "rgba(255,255,255,.55)";
      ctx.beginPath();
      if (kind === "waveform") {
        const y = Math.round(ph - (ire / max) * ph) + .5;
        ctx.moveTo(padL, y); ctx.lineTo(W, y);
        ctx.textBaseline = ire === 0 ? "bottom" : "middle";
        ctx.textAlign = "right";
        ctx.fillText(ire + "%", padL - 3 * dpr, Math.max(7 * dpr, Math.min(ph, y)));
      } else {
        const x = Math.round(padL + (ire / max) * pw) + .5;
        ctx.moveTo(x, 0); ctx.lineTo(x, ph);
        ctx.textBaseline = "top";
        ctx.textAlign = ire === 0 ? "left" : "center";
        ctx.fillText(ire + "%", x, ph + 2 * dpr);
      }
      ctx.stroke();
    }
    ctx.textAlign = "left";
    if (!d) {
      ctx.fillStyle = "rgba(255,255,255,.45)";
      ctx.textBaseline = "middle";
      ctx.fillText(this.player.playing ? "Measuring…" : "No picture", padL + 8 * dpr, ph / 2);
    }
  }
}
