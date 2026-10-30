// Recorder (REC-01..16): live preview with on-set monitoring (meters, tally, guides, zebras,
// false colour, scopes, focus loupe), record button, timecode / slate / markers, storage you can
// trust, recording settings.

import { h, clear, icon, store, conn, run, sheet, seg, toggle, toast, fmtBytes, fmtRate, fmtDur, busy, errText,
         confirmBox, formBox } from "../core.js";
import { PreviewPlayer } from "../preview.js";
import { OnSet, GUIDES, ZOOMS } from "../onset.js";
import { frameMs } from "./gallery.js";

const QUALITY = [["low", "360p"], ["medium", "720p"], ["high", "1080p"]];
const PREF = "arstro.preview.on";
const CONTAINER = { mov: "PCM audio, opens in every editor", mkv: "PCM audio, survives a power cut",
                    mp4: "AAC audio, plays everywhere" };

/** 918 s -> "15 min"; 7380 -> "2 h 3 min" (REC-12: time left at the data rate). */
export function fmtLeft(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "–";
  if (sec < 60) return "< 1 min";
  const hh = Math.floor(sec / 3600), mm = Math.floor(sec / 60) % 60;
  return hh ? `${hh} h ${mm} min` : `${mm} min`;
}

/** The take a recording file belongs to: REC_x.arh / S1_SH2_T3_x_H264.mp4 -> its id. */
function takeIdOf(file) {
  return String(file || "").replace(/\.[^.]+$/, "").replace(/_(H265|H264.*|FFV1)$/, "");
}

function prefOn() {
  try { return localStorage.getItem(PREF) !== "0"; } catch (e) { return true; }
}
function setPref(on) {
  try { localStorage.setItem(PREF, on ? "1" : "0"); } catch (e) { /* private mode */ }
}

export default {
  id: "recorder", title: "Recorder", icon: "recorder",

  mount(root) {
    let rec = store.get("recorder") || {};
    let previewOn = prefOn();
    let pState = { state: "stopped", detail: "" };
    let elapsedAt = { value: 0, t: 0 };
    let pending = false;
    let lastFile = null, lastMeta = null, lastAv = null;       // the last take's A/V sync (REC-09)

    // ------------------------------------------------------------ preview
    const msgIcon = h("span");
    const msgTitle = h("b");
    const msgDetail = h("small");
    const msgAction = h("div");
    const msg = h("div.msg", null, msgIcon, msgTitle, msgDetail, msgAction);
    const pill = h("span.glass", null, h("span.dot"), h("span", null, "Preview"));
    const res = h("span.glass.hidden");
    const recBadge = h("span.glass.rec.hidden", null, h("span.dot"), h("span.num", null, "REC"));
    const fsBtn = h("button.btn.icon", { title: "Full screen", "aria-label": "Full screen",
                                         onclick: () => (document.fullscreenElement ? document.exitFullscreen()
                                                                                    : stage.requestFullscreen?.()) }, icon("expand"));
    const tcBadge = h("span.glass.num.tc-badge.hidden");
    const ovTl = h("div.ov.tl", null, pill);
    const ovBr = h("div.ov.br", null, fsBtn);
    const box = h("div.preview", null, msg, ovTl, h("div.ov.tr", null, res),
                  h("div.ov.bl", null, recBadge, tcBadge), ovBr);

    let onset = null;
    const player = new PreviewPlayer(box, {
      onState: (state, detail) => { pState = { state, detail }; renderPreview(); },
      onConfig: () => { renderPreview(); onset?.draw(); onset?.place(); },
      onPlaying: () => { renderPreview(); onset?.draw(); onset?.drawScope(); onset?.place(); },
      onOpen: () => onset?.reset(),
      onClose: () => onset?.reset(),
      onLevels: (m) => onset?.levels(m),
      onAnalysis: (b) => onset?.analysis(b),
      onZoom: (z) => onset?.onZoom(z),
      onLoupe: (on) => onset?.loupeShowing(on),
    });
    // REC-10/13/14: meters, tally, guides, zebras, false colour, scopes, focus loupe
    onset = new OnSet(box, player);
    ovTl.append(onset.lutTag);
    const stage = h("div.live-stage", null, box, onset.strip);

    const quality = seg(QUALITY, (store.get("recorder.settings") || {}).preview?.quality || "medium",
      (q) => run("recorder.settings.set", { settings: { preview: { quality: q } } }));
    const previewToggle = toggle(previewOn, (on) => {
      previewOn = on;
      setPref(on);
      syncPlayer();
    }, { label: "Live preview" });
    const viewers = h("span.muted.num", { style: { fontSize: "13px" } });
    const previewBar = h("div.row.wrap", { style: { marginTop: "12px", justifyContent: "space-between" } },
      h("label.row", { style: { gap: "10px", cursor: "pointer" } }, previewToggle, h("span.dim", null, "Live preview")),
      h("div.row", null, viewers, quality));

    // ------------------------------------------------------ record control
    const recBtn = h("button.rec-btn", { "aria-label": "Record", onclick: () => toggleRecording() }, h("i"));
    const recTime = h("div.rec-time", null, "0:00");
    const recHint = h("div.rec-hint");
    // REC-11/15: timecode, the next take's name, markers
    const tcLine = h("div.rec-tc.num");
    const markCount = h("span.muted.num");
    const markBtn = h("button.btn.sm", { title: "Set a marker now (Shift-click or hold: with a label)",
                                         onclick: (e) => mark(e, e.shiftKey),
                                         oncontextmenu: (e) => { e.preventDefault(); mark(e, true); } },
                      icon("flag"), "Marker");
    const markRow = h("div.row.mark-row.hidden", null, markBtn, markCount);
    const clockWarn = h("div.clock-warn.hidden", null, icon("clock"),
      h("span", null, "Board clock not synced (NTP): the timecode may not match other devices"));
    const recMain = h("div.card.rec-main", null, recBtn, recTime, tcLine, recHint, markRow, clockWarn);

    const modeText = h("div.ellipsis", { style: { fontWeight: 600 } });
    const modeSub = h("div.muted.ellipsis", { style: { fontSize: "12.5px" } });
    const modeCard = h("div.card.mode-card", { role: "button", tabindex: "0", onclick: () => openSettings(),
                                               onkeydown: (e) => e.key === "Enter" && openSettings() },
      h("div.ico", null, icon("sliders")), h("div.grow", null, modeText, modeSub), icon("chevron", "muted"));

    const statsBox = h("div.metrics.rec-stats");
    const whyBox = h("div.why.hidden");
    // REC-12: the disk - speed measured or not, a warning, the test
    const diskText = h("div.grow.disk-text");
    const diskBtn = h("button.btn.sm", { onclick: (e) => diskTest(e.currentTarget) }, icon("disk"), "Test disk");
    const diskWarn = h("div.disk-warn.hidden");
    const diskBox = h("div.disk-line", null, h("div.row", null, diskText, diskBtn), diskWarn);
    const statsCard = h("div.card", null, h("div.card-title", null, "Status"), statsBox, diskBox, whyBox);
    const lastCard = h("div.card.hidden");

    const headSub = h("div.sub", null, "HDMI input · record and watch live");
    const head = h("div.view-head", null, h("div", null, h("h1", null, "Recorder"), headSub));
    root.append(head, h("div.rec-layout", null,
      h("div", null, stage, previewBar),
      h("div.rec-panel", null, recMain, modeCard, statsCard, lastCard)));

    // ------------------------------------------------------------- render
    function renderPreview() {
      const sig = rec.signal || {};
      const st = pState.state;
      const playing = player.playing;
      box.classList.toggle("playing", playing && previewOn);
      pill.className = "glass" + (playing ? " live" : "");
      pill.lastChild.textContent = playing ? "Live" : !previewOn ? "Paused" : sig.present ? "Preview" : "No signal";
      const cfg = player.config;
      res.classList.toggle("hidden", !(sig.present && sig.width));
      res.textContent = sig.present ? `${sig.width}×${sig.height} · ${Math.round(sig.fps)} fps` +
        (cfg && playing ? ` → ${cfg.height}p` : "") : "";
      clear(msgAction);
      let ic = "recorder", title = "", detail = "";
      if (!rec.available) {
        ic = "alert"; title = "Recorder not running"; detail = rec.signal?.why || "The recorder process is starting or disabled.";
      } else if (!previewOn) {
        ic = "pause"; title = "Preview paused"; detail = "The picture is not streamed. Recording works without it.";
        msgAction.append(h("button.btn.sm", { onclick: () => { previewToggle.set(true); previewOn = true; setPref(true); syncPlayer(); } },
                           icon("play"), "Resume"));
      } else if (st === "unsupported") {
        ic = "alert"; title = "Can't play here"; detail = pState.detail;
      } else if (!sig.present) {
        ic = "no-signal";
        title = rec.camera ? "No camera picture" : "No HDMI signal";
        detail = rec.camera ? `${rec.camera.split("@")[0]} is not connected or not sending.`
                            : sig.why || "Connect a source to the HDMI input.";
      } else if (st === "no-signal") {
        ic = "no-signal"; title = "Signal lost"; detail = pState.detail;
      } else {
        ic = null; title = "Starting preview…"; detail = "";
      }
      clear(msgIcon, ic ? icon(ic) : h("span.spin", { style: { width: "28px", height: "28px" } }));
      msgTitle.textContent = title;
      msgDetail.textContent = detail;
      viewers.textContent = rec.preview ? `${rec.preview.viewers} watching` : "";
    }

    function tickElapsed() {
      const r = rec.recording || {};
      if (!r.active) return;
      const now = performance.now();
      const v = elapsedAt.value + (r.stopping ? 0 : (now - elapsedAt.t) / 1000);
      recTime.textContent = fmtDur(v);
      recBadge.lastChild.textContent = "REC " + fmtDur(v);
    }

    function renderRecord() {
      const r = rec.recording || {};
      const sig = rec.signal || {};
      const active = !!r.active;
      recBtn.classList.toggle("on", active);
      recBtn.classList.toggle("wait", pending || !!r.stopping);
      recBtn.disabled = !rec.available || (!active && !sig.present) || pending || !!r.stopping;
      recBtn.setAttribute("aria-label", active ? "Stop recording" : "Start recording");
      recTime.classList.toggle("on", active);
      recBadge.classList.toggle("hidden", !active);
      const take = rec.take || {};
      const tc = r.tc || take.tc;
      tcBadge.classList.toggle("hidden", !(active && tc));
      tcBadge.textContent = tc ? "TC " + tc : "";
      if (active) {
        elapsedAt = { value: r.elapsed || 0, t: performance.now() };
        tickElapsed();
        recHint.textContent = r.stopping ? "Finishing the file…" : r.file || "";
        tcLine.textContent = tc ? "Start TC " + tc : "Timecode…";
      } else {
        recTime.textContent = "0:00";
        recHint.textContent = !rec.available ? "Recorder not running" : !sig.present ? "Waiting for a signal"
          : pending ? "Starting…" : "Tap to record";
        tcLine.textContent = !take.next ? "" : take.next === "REC" ? "Next: REC (no slate)" : "Next: " + take.next;
      }
      if (active && r.av) lastAv = { av: r.av, fps: sig.fps };
      markRow.classList.toggle("hidden", !active);
      markBtn.disabled = !!r.stopping;
      markCount.textContent = take.markers ? `${take.markers} marker${take.markers === 1 ? "" : "s"}` : "";
      clockWarn.classList.toggle("hidden", rec.clock_synced !== false);
      modeText.textContent = rec.mode_text || "–";
      const s = store.get("recorder.settings") || {};
      headSub.textContent = (rec.source_title || "HDMI input") + " · record and watch live";
      // the EDID only matters for the HDMI input (a USB camera or the test pattern has none)
      modeSub.textContent = [s.audio?.record ? "Audio on" : "Audio off",
                             rec.source === "hdmi" ? "EDID " + (s.edid || "–") : null].filter(Boolean).join(" · ");

      const disk = rec.disk || {};
      const sto = rec.storage || {};
      const m = (k, v, small) => h("div.metric", null, h("span.k", null, k), h("span.v", null, v, small ? h("small", null, small) : null));
      const left = sto.seconds_left !== undefined && sto.seconds_left !== null
        ? m("Time left", fmtLeft(sto.seconds_left), fmtBytes(sto.free ?? disk.free))
        : m("Free", fmtBytes(disk.free));
      if (active) {
        clear(statsBox,
          m("Size", fmtBytes(r.size)), m("Data rate", fmtRate(r.rate)),
          m("Dropped", String(r.drops || 0)), left);
      } else {
        const fmt = sig.present ? `${sig.width}×${sig.height}` : "–";
        clear(statsBox,
          m("Signal", fmt, sig.present ? `${Math.round(sig.fps)} fps` : ""),
          m("Format", sig.format || "–"),
          left,
          m("Audio", rec.capture?.audio ? "Yes" : rec.caps?.audio ? "Ready" : "No"));
      }
      renderDisk(sto, active);
      const why = !active && rec.available && !sig.present ? sig.why : "";
      whyBox.classList.toggle("hidden", !why);
      whyBox.textContent = why || "";
      const last = rec.last;
      lastCard.classList.toggle("hidden", !last || active);
      if (last && last.file && last.file !== lastFile) {      // the take's own A/V sync (REC-09)
        lastFile = last.file;
        lastMeta = null;
        conn.call("gallery.get", { take: takeIdOf(last.file) })
          .then((t) => { if (lastFile === last.file) { lastMeta = t.meta || null; renderRecord(); } })
          .catch(() => {});
      }
      if (last && !active) {
        const meta = lastMeta || {};
        const drift = meta.drift_ms ?? (lastAv && lastAv.av.drift_ms);
        const fms = meta.rate ? frameMs(meta.rate) : frameMs(lastAv && lastAv.fps);
        const off = drift !== undefined && drift !== null && Math.abs(drift) > fms;
        clear(lastCard,
          h("div.card-title", null, "Last recording"),
          h("div.row", null,
            h("div.grow", null,
              h("div.ellipsis", { style: { fontWeight: 550 } }, last.file || "–"),
              h("div.muted", { style: { fontSize: "12.5px" } },
                [last.duration ? fmtDur(last.duration) : null, last.size ? fmtBytes(last.size) : null, last.reason]
                  .filter(Boolean).join(" · "))),
            h("a.btn.sm", { href: "#/gallery" }, "Gallery")),
          meta.tc || meta.markers?.length ? h("div.muted.num", { style: { fontSize: "12.5px", marginTop: "6px" } },
            [meta.tc ? "TC " + meta.tc : null, meta.markers?.length ? `${meta.markers.length} marker${meta.markers.length === 1 ? "" : "s"}` : null]
              .filter(Boolean).join(" · ")) : null,
          off ? h("div.av-warn", null, icon("alert"),
            `A/V sync: audio and video differ by ${Math.round(Math.abs(drift))} ms (more than a frame)`) : null,
          last.error ? h("div", { style: { color: "var(--warn)", fontSize: "12.5px", marginTop: "8px" } }, last.error) : null);
      }
    }

    function renderDisk(sto, active) {
      const mbs = (b) => Math.round(b / 1e6) + " MB/s";
      const parts = [];
      if (sto.speed) parts.push(h("span", null, "Disk " + mbs(sto.speed)));
      else parts.push(h("span.muted", null, "Disk speed not measured"));
      if (sto.need) parts.push(h("span.muted", null, ` · needs ${mbs(sto.need)}`));
      if (sto.ok === true) parts.push(" ", h("span.tag.ok", null, "fast enough"));
      if (sto.ok === false) parts.push(" ", h("span.tag.rec", null, "too slow"));
      clear(diskText, parts);
      diskBtn.disabled = !!sto.testing || active || !rec.available;
      diskBtn.lastChild.textContent = sto.testing ? "Testing…" : "Test disk";
      diskWarn.classList.toggle("hidden", !sto.warning);
      diskWarn.textContent = sto.warning || "";
    }

    async function diskTest(btn) {
      const r = await busy(btn, () => conn.call("recorder.disk_test", {}, 180000));
      if (r) toast(`Disk: ${Math.round(r.speed / 1e6)} MB/s` + (r.figures?.ok === false ? " - too slow for this format" : ""),
                   r.figures?.ok === false ? "err" : "ok");
    }

    async function mark(e, withLabel) {
      let label = "";
      if (withLabel) {
        const v = await formBox({ title: "Marker", ok: "Set marker", text: "At the moment you press Set marker.",
                                  fields: [{ name: "label", label: "Label", placeholder: "e.g. boom in shot" }] });
        if (!v) return;
        label = v.label.trim();
      }
      const m = await busy(e.currentTarget || markBtn, () => conn.call("recorder.mark", label ? { label } : {}));
      if (m) toast(`Marker at ${m.tc}` + (m.label ? ` · ${m.label}` : ""), "ok");
    }

    function syncPlayer() {
      const want = previewOn && rec.available && document.visibilityState === "visible";
      if (want) player.start();
      else { player.stop(); onset.reset(); }               // a paused preview has no loupe
      renderPreview();
    }

    async function toggleRecording() {
      const active = rec.recording?.active;
      pending = true;
      renderRecord();
      try {
        try {
          await conn.call(active ? "recorder.stop" : "recorder.start", {}, 60000);
        } catch (e) {
          // REC-12: a disk measured too slow refuses the start - unless the user records anyway
          const why = errText(e);
          if (active || !/^disk too slow/i.test(why)) throw e;
          const ok = await confirmBox({ title: "Record anyway?", danger: true, ok: "Record anyway",
            text: "The disk is too slow: " + why.replace(/^disk too slow:\s*/i, "").replace(/\s*\(record anyway: force\)\s*$/, "") +
                  ". Frames may be dropped." });
          if (ok) await conn.call("recorder.start", { force: true }, 60000);
        }
      } catch (e) {
        toast(errText(e), "err");
      } finally {
        pending = false;
        renderRecord();
      }
    }

    // ------------------------------------------------------------- wiring
    const offs = [
      store.on("recorder", (v) => {
        const was = rec.available;
        rec = v || {};
        onset.status(rec);
        renderRecord();
        renderPreview();
        if (was !== rec.available) syncPlayer();
      }),
      store.on("recorder.settings", (s) => { quality.set(s.preview?.quality); onset.settings(s.monitor); renderRecord(); }),
    ];
    const timer = setInterval(tickElapsed, 250);
    const onVis = () => syncPlayer();
    document.addEventListener("visibilitychange", onVis);
    syncPlayer();

    return () => {
      offs.forEach((f) => f());
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
      onset.destroy();
      player.destroy();
    };
  },
};

// ------------------------------------------------------------ settings
export function openSettings() {
  const body = h("div");
  const s = sheet({ title: "Recording settings", body, wide: false });
  let header = null;                   // the board's header pins, for the GPIO picker (REC-16)
  const render = (st) => {
    if (s.closed) return;
    const rec = store.get("recorder") || {};
    const caps = rec.caps || {};
    const set = (patch) => run("recorder.settings.set", { settings: patch });
    const row = (title, sub, ctl) => h("div.set-row", null, h("div.txt", null, h("b", null, title), sub ? h("small", null, sub) : null), ctl);
    const scroll = body.parentElement ? body.parentElement.scrollTop : 0;

    const h265 = st.h265 || {}, raw = st.raw || {};
    const cap = (q) => (q || "").charAt(0).toUpperCase() + (q || "").slice(1);
    // a copy's sub-section: header = chevron, name, what it is (or its options when on), switch
    const copySec = (key, title, what, summary, on, onToggle, disabled, options) => {
      const open = OPEN_COPIES.has(key);
      let sec, head;
      const flip = () => {                 // in place, so the body eases open / shut
        const o = !OPEN_COPIES.has(key);
        if (o) OPEN_COPIES.add(key); else OPEN_COPIES.delete(key);
        saveOpen();
        sec.classList.toggle("open", o);
        head.setAttribute("aria-expanded", String(o));
      };
      const sw = toggle(on, (v) => { if (v) { OPEN_COPIES.add(key); saveOpen(); } onToggle(v); }, { disabled, label: title + " copy" });
      head = h("div.copy-head", { role: "button", tabindex: "0", "aria-expanded": String(open), "data-copy": key,
        onclick: (e) => { if (!e.target.closest(".switch")) flip(); },
        onkeydown: (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) { e.preventDefault(); flip(); } } },
        icon("chevron", "copy-chev"),
        h("div.txt", null, h("b", null, title), h("small", null, on && summary ? summary : what)),
        sw);
      sec = h("div.copy-sec" + (open ? ".open" : "") + (on ? ".on" : ""), null, head,
        h("div.copy-body", null, h("div.copy-inner", null, options)));
      return sec;
    };
    const bitrateVal = h("span.num.dim", { style: { minWidth: "70px", textAlign: "right" } }, h265.bitrate + " Mb/s");
    const bitrate = h("input", { type: "range", min: "2", max: "200", step: "1", value: String(h265.bitrate),
      oninput: (e) => { bitrateVal.textContent = e.target.value + " Mb/s"; },
      onchange: (e) => set({ h265: { bitrate: Number(e.target.value) } }) });

    const storage = h("input.input", { value: st.storage || "", spellcheck: "false" });
    const saveStorage = h("button.btn.sm", { onclick: (e) => run("recorder.settings.set", { settings: { storage: storage.value } },
                                                               { ok: "Storage folder saved", btn: e.currentTarget }) }, "Save");
    const sim = (store.get("recorder") || {}).simulate;
    const simInput = h("input.input", { value: sim || "1920x1080@30", spellcheck: "false", style: { maxWidth: "160px" } });
    // CAM-01/02: HDMI input, USB (V4L2) cameras or the test pattern
    const sourceBox = h("div.set-group", null, row("Input", "Looking for cameras…", h("span.spin")));
    const loadSources = async () => {
      let r;
      try { r = await conn.call("camera.sources"); } catch (e) { return clear(sourceBox, row("Input", errText(e), null)); }
      const pick = (src, extra) => busy(null, async () => {
        await conn.call("camera.select", { source: src, ...extra }, 60000);
        toast("Camera source changed", "ok");
        loadSources();
      });
      clear(sourceBox, r.sources.map((src) => {
        const on = r.current === src.id || (src.kind === "test" && r.current.startsWith("test")) ||
                   (src.kind === "v4l2" && r.current.startsWith(src.id));
        let mode = null;
        const modeSel = src.modes && src.modes.length ? h("select.input", { style: { width: "auto", maxWidth: "170px" }, onchange: (e) => { mode = e.target.value || null; } },
          h("option", { value: "" }, "Best mode"), src.modes.map((m) => h("option", { value: m }, m))) : null;
        return row(src.title, [src.note, on ? "in use" : null].filter(Boolean).join(" · "),
          h("div.row", null, src.kind === "test" ? simInput : modeSel,
            h("button.btn.sm", { class: on ? "primary" : "", disabled: !src.available,
                                 onclick: () => pick(src.id, src.kind === "test" ? { spec: simInput.value } : mode ? { mode } : {}) },
              on ? "Selected" : "Use")));
      }));
    };

    clear(body,
      h("div.set-group-title", null, "Format"),
      h("div.set-group", null,
        row("Recording format", st.mode === "h265" ? "Hardware H.265, small files, real time" : "Uncompressed .arh, exact picture, very large",
          seg([["h265", "H.265"], ["raw", "RAW"]], st.mode, (v) => set({ mode: v }), { disabled: { h265: !caps.vpu_h265 } }))),

      st.mode === "h265" ? h("div.set-group", null,
        h("div.set-row", null, h("div.txt", null, h("b", null, "Bitrate")), bitrateVal,
          h("div", { style: { width: "100%" } }, bitrate)),
        row("Rate control", "CBR keeps a steady rate; VBR saves space on calm scenes",
          seg([["cbr", "CBR"], ["vbr", "VBR"]], h265.rc, (v) => set({ h265: { rc: v } }))),
        row("Keyframe interval", "Shorter = easier editing, bigger files",
          seg([[0.5, "0.5 s"], [1, "1 s"], [2, "2 s"], [5, "5 s"]], Number(h265.gop), (v) => set({ h265: { gop: v } }))),
        row("Container", (h265.container || "mov").toUpperCase() + ": " + (CONTAINER[h265.container] || ""),
          seg([["mov", "MOV"], ["mkv", "MKV"], ["mp4", "MP4"]], h265.container, (v) => set({ h265: { container: v } }))))
      : h("div", null,
        h("div.set-group-title", null, "Extra copies"),
        // REC-04: one sub-section per kind of copy, H.264 - H.265 - FFV1; its switch in the header,
        // its options inside (the header opens them; switching a copy on opens it too)
        h("div.set-group.copies", null,
          copySec("h264", "H.264", "MP4 share copy that plays everywhere",
            raw.h264 ? [{ source: "Source size", "1080": "1080p", "720": "720p" }[raw.h264_scale], cap(raw.h264_quality)].join(" · ") : null,
            raw.h264, (v) => set({ raw: { h264: v } }), !caps.vpu_h264,
            [row("Size", "Smaller copies are faster to make and to send",
               seg([["source", "Source"], ["1080", "1080p"], ["720", "720p"]], raw.h264_scale, (v) => set({ raw: { h264_scale: v } }))),
             row("Quality", "Hardware encoder (VPU)", seg([["high", "High"], ["higher", "Higher"], ["max", "Max"]], raw.h264_quality,
                                                         (v) => set({ raw: { h264_quality: v } })))]),
          copySec("h265", "H.265", "Compact high-quality copy",
            raw.hq ? [raw.hq_engine === "x265" ? "x265 " + raw.x265_preset : "VPU", cap(raw.hq_quality),
                      raw.hq_chroma === "source" ? "source chroma" : "4:2:0"].join(" · ") : null,
            raw.hq, (v) => set({ raw: { hq: v } }), !caps.vpu_h265 && !caps.x265,
            [row("Encoder", raw.hq_engine === "x265" ? "CPU, best quality, slow" : "Hardware, about real time",
               seg([["vpu", "VPU"], ["x265", "x265"]], raw.hq_engine, (v) => set({ raw: { hq_engine: v } }),
                   { disabled: { vpu: !caps.vpu_h265, x265: !caps.x265 } })),
             row("Quality", null, seg([["high", "High"], ["higher", "Higher"], ["max", "Max"]], raw.hq_quality,
                                      (v) => set({ raw: { hq_quality: v } }))),
             raw.hq_engine === "x265" ? row("x265 preset", "Slower presets compress better",
               selectEl(["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"], raw.x265_preset,
                        (v) => set({ raw: { x265_preset: v } }))) : null,
             row("Chroma", "4:2:0 plays everywhere; source keeps 4:2:2 / 4:4:4",
               seg([["420", "4:2:0"], ["source", "Source"]], raw.hq_chroma, (v) => set({ raw: { hq_chroma: v } })))]),
          copySec("ffv1", "FFV1", "Lossless: the exact picture at a fraction of the RAW size",
            raw.ffv1 ? [raw.ffv1_engine === "gpu" ? "GPU" : "CPU", raw.ffv1_replace_raw ? "replaces the RAW" : "keeps the RAW"].join(" · ") : null,
            raw.ffv1, (v) => set({ raw: { ffv1: v } }), !caps.ffv1 && !caps.gpu_ffv1,
            [row("Engine", "GPU is slower but leaves the CPU free",
               seg([["cpu", "CPU"], ["gpu", "GPU"]], raw.ffv1_engine, (v) => set({ raw: { ffv1_engine: v } }),
                   { disabled: { cpu: !caps.ffv1, gpu: !caps.gpu_ffv1 } })),
             row("Delete RAW after a verified copy",
               "Every frame and audio sample is compared byte for byte first; if anything differs the RAW stays",
               toggle(raw.ffv1_replace_raw, (v) => set({ raw: { ffv1_replace_raw: v } })))]),
          (raw.h264 || raw.hq || raw.ffv1) ? row("Make copies", "During recording needs more CPU/VPU",
            seg([["during", "During"], ["after", "After"]], raw.when, (v) => set({ raw: { when: v } }))) : null)),

      audioGroup(st, rec, set, row),
      slateGroup(st, rec, set, row, () => render(store.get("recorder.settings") || st)),
      monitorGroup(st, rec, set, row),

      h("div.set-group-title", null, "Input"),
      h("div.set-group", null,
        row("Preview quality", "Shared by every viewer", seg(QUALITY, st.preview?.quality, (v) => set({ preview: { quality: v } }))),
        row("EDID", "What the board tells the source it supports",
          seg([["4k60", "4K60"], ["4k30", "4K30"], ["1080p", "1080p"], ["keep", "Keep"]], st.edid, (v) => set({ edid: v })))),

      h("div.set-group-title", null, "Storage"),
      h("div.set-group", null,
        h("div.set-row", null, h("div.field.grow", null, h("label", null, "Folder on the Pi"),
          h("div.row", null, storage, saveStorage))),
        row("Storage guard", "Refuse a take when the measured disk is too slow for the format (Test disk on the Live page)",
          toggle(st.storage_guard !== false, (v) => set({ storage_guard: v }), { label: "Storage guard" }))),

      gpioGroup(st, rec, set, row, header),

      h("div.set-group-title", null, "Source"),
      sourceBox);
    loadSources();
    if (body.parentElement) body.parentElement.scrollTop = scroll;
  };
  // the parts that come from the status (inputs, LUTs, GPIO problems, the signal) re-render the
  // sheet only when they change; the next take's name is updated in place
  const fp = (r) => JSON.stringify([r.audio_inputs, r.monitor?.luts, r.monitor?.lut_error, r.monitor?.bands,
                                    [r.gpio?.error, r.gpio?.button?.error, r.gpio?.button?.ok, r.gpio?.tally?.error, r.gpio?.tally?.ok],
                                    r.signal?.width, r.signal?.height, r.caps]);
  let lastFp = fp(store.get("recorder") || {});
  const offs = [store.on("recorder.settings", render), store.on("recorder", (r) => {
    s.body.querySelectorAll("[data-next-take]").forEach((el) => { el.textContent = nextName(r.take?.next); });
    const f = fp(r || {});
    if (f !== lastFp) { lastFp = f; render(store.get("recorder.settings") || {}); }
  }, { now: false })];
  const ioOn = (conn.hello?.modules || []).some((m) => m.name === "io" && m.state !== "disabled");
  if (ioOn) {
    conn.call("io.gpio.header").then((r) => {
      header = (r.pins || []).filter((p) => p.gpio && p.chip);
      if (header.length && !s.closed) render(store.get("recorder.settings") || {});
    }).catch(() => {});
  }
  const origClose = s.close;
  s.close = () => { offs.forEach((f) => f()); origClose(); };
  return s;
}

// which copy sub-sections this page has open (view state; remembered in this browser only)
const OPEN_COPIES = new Set((() => { try { return JSON.parse(localStorage.getItem("rec.copies.open") || "[]"); } catch { return []; } })());
function saveOpen() { try { localStorage.setItem("rec.copies.open", JSON.stringify([...OPEN_COPIES])); } catch { /* private mode */ } }

// ------------------------------------------------------- film work settings
function audioGroup(st, rec, set, row) {
  const caps = rec.caps || {};
  const inputs = rec.audio_inputs || [];
  const cur = st.audio?.source || "hdmi";
  const opts = inputs.map((i) => [i.id, i.name + (i.usb ? " (USB)" : "")]);
  if (!inputs.some((i) => i.id === cur)) opts.push([cur, cur + " (not connected)"]);
  return [
    h("div.set-group-title", null, "Audio"),
    h("div.set-group", null,
      row("Record audio", caps.audio ? "Kept as it comes: 16-bit 48 kHz PCM (AAC in MP4)" : "No audio device",
        toggle(st.audio?.record, (v) => set({ audio: { record: v } }), { disabled: !caps.audio, label: "Record audio" })),
      row("Audio input", "The HDMI input, or an audio interface (e.g. USB with XLR microphones)",
        h("select.input.audio-src", { style: { width: "auto", maxWidth: "240px" }, "aria-label": "Audio input",
                                      onchange: (e) => set({ audio: { source: e.target.value } }) },
          opts.map(([v, l]) => h("option", { value: v, selected: v === cur }, l))))),
  ];
}

/** The next take's file name: the slate's part (S12A_SH3_T04, or REC) + the start's date and time. */
function nextName(prefix) {
  return prefix ? prefix + "_<date>_<time>" : "–";
}

function slateGroup(st, rec, set, row, reset) {
  const sl = st.slate || {};
  const commit = async (patch) => { if (!(await set({ slate: patch }))) reset(); };
  const text = (key, label, ph) => h("div.field", null, h("label", null, label),
    h("input.input", { value: sl[key] || "", placeholder: ph, maxlength: "16", spellcheck: "false", autocomplete: "off",
                       "data-slate": key, pattern: "[A-Za-z0-9\\-]*",
                       onchange: (e) => commit({ [key]: e.target.value.trim() }) }));
  return [
    h("div.set-group-title", null, "Slate"),
    h("div.set-group", null,
      h("div.set-row.slate-row", null,
        text("scene", "Scene", "e.g. 12A"), text("shot", "Shot", "e.g. 3"),
        h("div.field", null, h("label", null, "Take"),
          h("input.input", { type: "number", min: "1", max: "999", value: String(sl.take || 1), "data-slate": "take",
                             onchange: (e) => commit({ take: Number(e.target.value) || 1 }) }))),
      row("Count up after each take", "The take number goes up by one when a take ends",
        toggle(sl.auto !== false, (v) => set({ slate: { auto: v } }), { label: "Count up after each take" })),
      h("div.set-row", null, h("div.txt", null, h("b", null, "Next take"),
        h("small.mono", { "data-next-take": "" }, nextName(rec.take?.next))))),
  ];
}

function monitorGroup(st, rec, set, row) {
  const m = st.monitor || {};
  const mon = rec.monitor || {};
  const caps = rec.caps || {};
  const mset = (patch) => set({ monitor: patch });
  // the loupe magnifies what it covers, cut from the full frame (not the preview)
  const sig = rec.signal || {};
  const zoomSub = "Click the live picture for a magnifying glass" +
    (sig.width ? ` · cut from the full ${sig.width}×${sig.height} frame` : "");
  const levelVal = h("span.num.dim", { style: { minWidth: "48px", textAlign: "right" } }, (m.zebra_level || 95) + " %");
  const level = h("input", { type: "range", min: "50", max: "109", step: "1", value: String(m.zebra_level || 95),
    "aria-label": "Zebra level", oninput: (e) => { levelVal.textContent = e.target.value + " %"; },
    onchange: (e) => mset({ zebra_level: Number(e.target.value) }) });
  const legend = (mon.bands || []).map((b) => h("span.band", { title: `${b.from} – ${b.to} %` },
    h("i", { style: { background: b.colour } }), b.meaning));
  // LUT: off, the built-ins, the user's .cube files
  const luts = mon.luts || [];
  const userLuts = luts.filter((l) => !l.builtin);
  const file = h("input", { type: "file", accept: ".cube,text/plain", class: "hidden",
                            onchange: (e) => addLut(e.target.files[0], e.target) });
  const addBtn = h("button.btn.sm", { onclick: () => file.click(), disabled: !caps.lut }, icon("plus"), "Add .cube…");
  async function addLut(f, input) {
    input.value = "";
    if (!f) return;
    const name = f.name.replace(/\.cube$/i, "").replace(/[^A-Za-z0-9 ._+-]/g, "_").replace(/^[^A-Za-z0-9]+/, "").slice(0, 60) || "LUT";
    if (f.size > 16 << 20) return toast("A .cube file of more than 16 MB is not a monitoring LUT", "err");
    const r = await busy(addBtn, async () => conn.call("recorder.lut.add", { name, text: await f.text() }, 30000));
    if (r && await set({ monitor: { lut: r.id } })) toast(`LUT ${name} added - the preview shows it`, "ok");
  }
  return [
    h("div.set-group-title", null, "Monitoring"),
    h("div.set-group.mon-set", null,
      h("div.hint", { style: { padding: "10px 0 2px" } }, "On the live picture, the same for every viewer. Never in the recording."),
      row("Frame guides", "Where the picture will be cut",
        h("select.input", { style: { width: "auto", minWidth: "110px" }, "aria-label": "Frame guides",
                            onchange: (e) => mset({ guides: e.target.value }) },
          GUIDES.map(([v, l]) => h("option", { value: v, selected: v === (m.guides || "off") }, l)))),
      row("Safe areas", "Action 93 % · title 90 %", toggle(m.safe, (v) => mset({ safe: v }), { label: "Safe areas" })),
      row("Centre cross", null, toggle(m.center, (v) => mset({ center: v }), { label: "Centre cross" })),
      row("Zebras", "Stripes where the picture is at or over the level",
        toggle(m.zebra, (v) => mset({ zebra: v }), { label: "Zebras", disabled: !caps.analysis })),
      m.zebra ? h("div.set-row", null, h("div.txt", null, h("b", null, "Zebra level")), levelVal,
        h("div", { style: { width: "100%" } }, level)) : null,
      row("False colour", "Exposure as colours", toggle(m.false_color, (v) => mset({ false_color: v }),
                                                        { label: "False colour", disabled: !caps.analysis })),
      m.false_color && legend.length ? h("div.set-row.legend", null, legend) : null,
      row("Scope", "Luma of the full captured frame",
        seg([["off", "Off"], ["histogram", "Histogram"], ["waveform", "Waveform"]], m.scope || "off", (v) => mset({ scope: v }),
            { disabled: caps.analysis ? {} : { histogram: true, waveform: true } })),
      row("Audio meters", "Peak and RMS per channel", toggle(m.meters !== false, (v) => mset({ meters: v }), { label: "Audio meters" })),
      row("Focus loupe", zoomSub,
        seg(ZOOMS.map((z) => [z, z + "×"]), Number(m.zoom) || 3, (v) => mset({ zoom: v }))),
      row("LUT", caps.lut === false ? "Needs numpy on the board" : "Shows log footage as normal video (preview only)",
        h("div.row", null,
          h("select.input.lut-sel", { style: { width: "auto", maxWidth: "220px" }, "aria-label": "LUT", disabled: caps.lut === false,
                                      onchange: (e) => mset({ lut: e.target.value }) },
            h("option", { value: "off", selected: (m.lut || "off") === "off" }, "Off"),
            luts.map((l) => h("option", { value: l.id, selected: l.id === m.lut }, l.builtin ? l.name : l.id.replace(/^cube:/, "") + " (.cube)")),
            m.lut && m.lut !== "off" && !luts.some((l) => l.id === m.lut) ? h("option", { value: m.lut, selected: true }, m.lut + " (missing)") : null),
          addBtn, file)),
      userLuts.length ? h("div.set-row.lut-list", null, userLuts.map((l) => h("span.chip.lut-chip", null, l.id.replace(/^cube:/, ""),
        h("button.btn.ghost.icon.sm", { title: "Delete this LUT", "aria-label": "Delete LUT " + l.id,
          onclick: async (e) => {
            if (await confirmBox({ title: "Delete this LUT?", text: `${l.id.replace(/^cube:/, "")} is removed from the Pi.`, ok: "Delete", danger: true }))
              run("recorder.lut.delete", { lut: l.id }, { ok: "LUT deleted", btn: e.currentTarget });
          } }, icon("trash"))))) : null,
      mon.lut_error ? h("div.set-row.warn-row", null, icon("alert"), h("span", null, mon.lut_error)) : null),
  ];
}

function gpioGroup(st, rec, set, row, header) {
  const g = st.gpio || {};
  const gs = rec.gpio || {};
  const pinRow = (key, title, sub, state) => {
    const input = h("input.input.mono", { value: g[key] || "", placeholder: "gpiochip1:5", spellcheck: "false",
                                          autocomplete: "off", "data-gpio": key, style: { maxWidth: "180px" },
                                          onchange: (e) => set({ gpio: { [key]: e.target.value.trim() } }) });
    const pick = header && header.length ? h("select.input", { style: { width: "auto", maxWidth: "190px" }, "aria-label": title + " header pin",
      onchange: (e) => { if (e.target.value !== "") set({ gpio: { [key]: e.target.value } }); } },
      h("option", { value: "" }, "Header pin…"),
      h("option", { value: "-", disabled: true }, "──"),
      header.map((p) => {
        const v = `${p.chip}:${p.gpio.line}`;
        return h("option", { value: v, selected: v === g[key] },
          `Pin ${p.pin} · ${p.name}` + (p.held || (p.state && p.state !== "free") ? " (in use)" : ""));
      })) : null;
    const problem = state && state.error;
    const info = !g[key] ? "Not used" : problem ? null
      : key === "button" ? `Ready · ${state?.presses || 0} press${state?.presses === 1 ? "" : "es"}`
      : state?.ok ? (state.on ? "Ready · on" : "Ready · off") : "Ready";
    return [
      h("div.set-row", null, h("div.txt", null, h("b", null, title), h("small", null, sub),
                                info ? h("small.gpio-state", { "data-gpio-state": key }, info) : null),
        h("div.row.wrap", null, input, pick)),
      problem ? h("div.set-row.warn-row", { "data-gpio-error": key }, icon("alert"), h("span", null, problem)) : null,
    ];
  };
  return [
    h("div.set-group-title", null, "Record button & tally"),
    h("div.set-group", null,
      gs.error && !gs.button ? h("div.set-row.warn-row", null, icon("alert"), h("span", null, gs.error)) : null,
      pinRow("button", "Record button", "A GPIO input: a push button to ground starts or stops recording", gs.button),
      row("Button active low", "Pulled up; pressed = low (a button to ground)",
        toggle(g.button_active_low !== false, (v) => set({ gpio: { button_active_low: v } }), { label: "Button active low" })),
      pinRow("tally", "Tally light", "A GPIO output: on while recording, blinks while a take is finished", gs.tally),
      row("Tally active low", "On = low (an LED to 3.3 V)",
        toggle(!!g.tally_active_low, (v) => set({ gpio: { tally_active_low: v } }), { label: "Tally active low" }))),
  ];
}

function selectEl(values, value, onChange) {
  const el = h("select.input", { style: { width: "auto", minWidth: "130px" }, onchange: (e) => onChange(e.target.value) },
    values.map((v) => h("option", { value: v, selected: v === value }, v)));
  return el;
}
