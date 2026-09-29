// Recorder (REC-01..08): live preview, record button, recording settings.

import { h, clear, icon, store, conn, run, sheet, seg, toggle, toast, fmtBytes, fmtRate, fmtDur, busy, errText } from "../core.js";
import { PreviewPlayer } from "../preview.js";

const QUALITY = [["low", "360p"], ["medium", "720p"], ["high", "1080p"]];
const PREF = "arstro.preview.on";

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
                                                                                    : box.requestFullscreen?.()) }, icon("expand"));
    const box = h("div.preview", null, msg, h("div.ov.tl", null, pill), h("div.ov.tr", null, res),
                  h("div.ov.bl", null, recBadge), h("div.ov.br", null, fsBtn));

    const player = new PreviewPlayer(box, {
      onState: (state, detail) => { pState = { state, detail }; renderPreview(); },
      onConfig: () => renderPreview(),
      onPlaying: () => renderPreview(),
    });

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
    const recMain = h("div.card.rec-main", null, recBtn, recTime, recHint);

    const modeText = h("div.ellipsis", { style: { fontWeight: 600 } });
    const modeSub = h("div.muted.ellipsis", { style: { fontSize: "12.5px" } });
    const modeCard = h("div.card.mode-card", { role: "button", tabindex: "0", onclick: () => openSettings(),
                                               onkeydown: (e) => e.key === "Enter" && openSettings() },
      h("div.ico", null, icon("sliders")), h("div.grow", null, modeText, modeSub), icon("chevron", "muted"));

    const statsBox = h("div.metrics.rec-stats");
    const whyBox = h("div.why.hidden");
    const statsCard = h("div.card", null, h("div.card-title", null, "Status"), statsBox, whyBox);
    const lastCard = h("div.card.hidden");

    const head = h("div.view-head", null,
      h("div", null, h("h1", null, "Recorder"), h("div.sub", null, "HDMI input · record and watch live")));
    root.append(head, h("div.rec-layout", null,
      h("div", null, box, previewBar),
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
      if (active) {
        elapsedAt = { value: r.elapsed || 0, t: performance.now() };
        tickElapsed();
        recHint.textContent = r.stopping ? "Finishing the file…" : r.file || "";
      } else {
        recTime.textContent = "0:00";
        recHint.textContent = !rec.available ? "Recorder not running" : !sig.present ? "Waiting for a signal"
          : pending ? "Starting…" : "Tap to record";
      }
      modeText.textContent = rec.mode_text || "–";
      const s = store.get("recorder.settings") || {};
      modeSub.textContent = [s.audio?.record ? "Audio on" : "Audio off", "EDID " + (s.edid || "–")].join(" · ");

      const disk = rec.disk || {};
      const m = (k, v, small) => h("div.metric", null, h("span.k", null, k), h("span.v", null, v, small ? h("small", null, small) : null));
      if (active) {
        clear(statsBox,
          m("Size", fmtBytes(r.size)), m("Data rate", fmtRate(r.rate)),
          m("Dropped", String(r.drops || 0)), m("Free", fmtBytes(disk.free)));
      } else {
        const fmt = sig.present ? `${sig.width}×${sig.height}` : "–";
        clear(statsBox,
          m("Signal", fmt, sig.present ? `${Math.round(sig.fps)} fps` : ""),
          m("Format", sig.format || "–"),
          m("Free", fmtBytes(disk.free)),
          m("Audio", rec.capture?.audio ? "Yes" : rec.caps?.audio ? "Ready" : "No"));
      }
      const why = !active && rec.available && !sig.present ? sig.why : "";
      whyBox.classList.toggle("hidden", !why);
      whyBox.textContent = why || "";
      const last = rec.last;
      lastCard.classList.toggle("hidden", !last || active);
      if (last && !active) {
        clear(lastCard,
          h("div.card-title", null, "Last recording"),
          h("div.row", null,
            h("div.grow", null,
              h("div.ellipsis", { style: { fontWeight: 550 } }, last.file || "–"),
              h("div.muted", { style: { fontSize: "12.5px" } },
                [last.duration ? fmtDur(last.duration) : null, last.size ? fmtBytes(last.size) : null, last.reason]
                  .filter(Boolean).join(" · "))),
            h("a.btn.sm", { href: "#/gallery" }, "Gallery")),
          last.error ? h("div", { style: { color: "var(--warn)", fontSize: "12.5px", marginTop: "8px" } }, last.error) : null);
      }
    }

    function syncPlayer() {
      const want = previewOn && rec.available && document.visibilityState === "visible";
      if (want) player.start(); else player.stop();
      renderPreview();
    }

    async function toggleRecording() {
      const active = rec.recording?.active;
      pending = true;
      renderRecord();
      try {
        await conn.call(active ? "recorder.stop" : "recorder.start", {}, 60000);
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
        renderRecord();
        renderPreview();
        if (was !== rec.available) syncPlayer();
      }),
      store.on("recorder.settings", (s) => { quality.set(s.preview?.quality); renderRecord(); }),
    ];
    const timer = setInterval(tickElapsed, 250);
    const onVis = () => syncPlayer();
    document.addEventListener("visibilitychange", onVis);
    syncPlayer();

    return () => {
      offs.forEach((f) => f());
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
      player.destroy();
    };
  },
};

// ------------------------------------------------------------ settings
export function openSettings() {
  const body = h("div");
  const s = sheet({ title: "Recording settings", body, wide: false });
  const render = (st) => {
    if (s.closed) return;
    const caps = (store.get("recorder") || {}).caps || {};
    const set = (patch) => run("recorder.settings.set", { settings: patch });
    const row = (title, sub, ctl) => h("div.set-row", null, h("div.txt", null, h("b", null, title), sub ? h("small", null, sub) : null), ctl);
    const scroll = body.parentElement ? body.parentElement.scrollTop : 0;

    const h265 = st.h265 || {}, raw = st.raw || {};
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
        row("Container", "MKV survives a power cut; MP4 plays everywhere",
          seg([["mp4", "MP4"], ["mkv", "MKV"]], h265.container, (v) => set({ h265: { container: v } }))))
      : h("div", null,
        h("div.set-group-title", null, "Extra copies"),
        h("div.set-group", null,
          row("High-quality H.265", "A compact copy made from the RAW file",
            toggle(raw.hq, (v) => set({ raw: { hq: v } }), { disabled: !caps.vpu_h265 && !caps.x265 })),
          raw.hq ? h("div", null,
            row("Encoder", raw.hq_engine === "x265" ? "CPU, best quality, slow" : "Hardware, about real time",
              seg([["vpu", "VPU"], ["x265", "x265"]], raw.hq_engine, (v) => set({ raw: { hq_engine: v } }),
                  { disabled: { vpu: !caps.vpu_h265, x265: !caps.x265 } })),
            row("Quality", null, seg([["high", "High"], ["higher", "Higher"], ["max", "Max"]], raw.hq_quality,
                                     (v) => set({ raw: { hq_quality: v } }))),
            raw.hq_engine === "x265" ? row("x265 preset", "Slower presets compress better",
              selectEl(["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"], raw.x265_preset,
                       (v) => set({ raw: { x265_preset: v } }))) : null,
            row("Chroma", "4:2:0 plays everywhere; source keeps 4:2:2 / 4:4:4",
              seg([["420", "4:2:0"], ["source", "Source"]], raw.hq_chroma, (v) => set({ raw: { hq_chroma: v } })))) : null,
          row("Lossless FFV1", "Exact copy at a fraction of the RAW size",
            toggle(raw.ffv1, (v) => set({ raw: { ffv1: v } }), { disabled: !caps.ffv1 && !caps.gpu_ffv1 })),
          raw.ffv1 ? row("FFV1 engine", "GPU is slower but leaves the CPU free",
            seg([["cpu", "CPU"], ["gpu", "GPU"]], raw.ffv1_engine, (v) => set({ raw: { ffv1_engine: v } }),
                { disabled: { cpu: !caps.ffv1, gpu: !caps.gpu_ffv1 } })) : null,
          raw.ffv1 ? row("Delete RAW after a verified copy",
            "Every frame and audio sample is compared byte for byte first; if anything differs the RAW stays",
            toggle(raw.ffv1_replace_raw, (v) => set({ raw: { ffv1_replace_raw: v } }))) : null,
          (raw.hq || raw.ffv1) ? row("Make copies", "During recording needs more CPU/VPU",
            seg([["during", "During"], ["after", "After"]], raw.when, (v) => set({ raw: { when: v } }))) : null)),

      h("div.set-group-title", null, "Input"),
      h("div.set-group", null,
        row("Record audio", caps.audio ? "HDMI audio" : "No HDMI audio device",
          toggle(st.audio?.record, (v) => set({ audio: { record: v } }), { disabled: !caps.audio })),
        row("Preview quality", "Shared by every viewer", seg(QUALITY, st.preview?.quality, (v) => set({ preview: { quality: v } }))),
        row("EDID", "What the board tells the source it supports",
          seg([["4k60", "4K60"], ["4k30", "4K30"], ["1080p", "1080p"], ["keep", "Keep"]], st.edid, (v) => set({ edid: v })))),

      h("div.set-group-title", null, "Storage"),
      h("div.set-group", null,
        h("div.set-row", null, h("div.field.grow", null, h("label", null, "Folder on the Pi"),
          h("div.row", null, storage, saveStorage)))),

      h("div.set-group-title", null, "Source"),
      sourceBox);
    loadSources();
    if (body.parentElement) body.parentElement.scrollTop = scroll;
  };
  const offs = [store.on("recorder.settings", render), store.on("recorder", () => {}, { now: false })];
  const origClose = s.close;
  s.close = () => { offs.forEach((f) => f()); origClose(); };
  return s;
}

function selectEl(values, value, onChange) {
  const el = h("select.input", { style: { width: "auto", minWidth: "130px" }, onchange: (e) => onChange(e.target.value) },
    values.map((v) => h("option", { value: v, selected: v === value }, v)));
  return el;
}
