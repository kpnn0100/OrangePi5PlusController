// Camera (PHN-05): the front or back camera live (ws/camera, H.264), torch, a photo now (shown and
// downloadable), and recording a video into the phone's Movies/Arstro.

import { h, clear, icon, op, run, seg, toast, errText, pref, q, fmtDur, busy, store } from "../core.js";
import { VideoView } from "../video.js";

let recording = null;           // {since, facing}: this page started a recording (no camera topic)

export default {
  mount(root) {
    let cams = [];
    let facing = pref.get("camera.facing", "back");
    let size = null;
    let paused = false;

    const msgIcon = h("span"), msgTitle = h("b"), msgDetail = h("small");
    const msg = h("div.msg", null, msgIcon, msgTitle, msgDetail);
    const pill = h("span.glass", null, h("span.dot"), h("span", null, "Camera"));
    const recPill = h("span.glass.rec.hidden", null, h("span.dot"), h("span"));
    const fsBtn = h("button.btn.icon", { "aria-label": "Full screen", onclick: () => document.fullscreenElement ? document.exitFullscreen() : stage.requestFullscreen?.().catch(() => {}) }, icon("expand"));
    const stage = h("div.stage.cam-stage", null, msg, h("div.ov.tl", null, pill, recPill), h("div.ov.br", null, fsBtn));
    const video = new VideoView(stage, {
      rotate: true,
      onState: () => render(),
      onConfig: (cfg) => {
        const side = [90, 270].includes(((Number(cfg.rotation) || 0) % 360 + 360) % 360);
        const w = side ? cfg.height : cfg.width, hh = side ? cfg.width : cfg.height;
        if (w && hh) { stage.style.aspectRatio = `${w} / ${hh}`; stage.style.maxWidth = `calc((100vh - 190px) * ${w} / ${hh})`; }
        render();
      },
      onMessage: (m) => { if (m.t === "error" && m.error) toast(m.error, "err"); },
    });

    const facingSeg = seg([["back", "Back"], ["front", "Front"]], facing, (f) => { facing = f; pref.set("camera.facing", f); pickSize(); restart(); }, { label: "Camera" });
    const sizeSel = h("select.input", { "aria-label": "Size", style: { width: "auto", minWidth: "130px", height: "38px" },
      onchange: () => { size = sizeSel.value; pref.set("camera.size", size); restart(); } });
    const pauseBtn = h("button.btn.sm", { onclick: () => { paused = !paused; paused ? video.stop() : restart(); renderBtns(); } });
    const head = h("div.view-head", null,
      h("div", null, h("h1", null, "Camera"), h("div.sub", null, "Live from the phone's camera")),
      h("div.actions", null, facingSeg, sizeSel, pauseBtn));

    const torchBtn = h("button.btn", { onclick: (e) => torch(e.currentTarget) });
    const shotBtn = h("button.btn.primary", { onclick: (e) => photo(e.currentTarget) }, icon("camera"), "Take photo");
    const recBtn = h("button.btn", { onclick: (e) => record(e.currentTarget) });
    const photoBox = h("div.photo-box.stack");
    const side = h("div.stack", null,
      h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, "Camera"),
        h("div.row.wrap", null, shotBtn, recBtn, torchBtn),
        h("div.hint", null, "Videos are saved on the phone in Movies/Arstro. Android shows the camera indicator while the camera is in use.")),
      photoBox);
    root.append(head, h("div.cam-layout", null, h("div", null, stage), side));

    function pickSize() {
      const cam = cams.find((c) => c.facing === facing);
      const sizes = (cam && cam.sizes) || [];
      const want = pref.get("camera.size", "1280x720");
      size = sizes.includes(want) ? want : (sizes.find((s) => parseInt(s, 10) <= 1280) || sizes[0] || null);
      clear(sizeSel, sizes.map((s) => h("option", { value: s, selected: s === size }, s.replace("x", " × "))));
      sizeSel.disabled = !sizes.length;
      facingSeg.querySelectorAll("button").forEach((b) => { b.disabled = cams.length > 0 && !cams.some((c) => c.facing === b.dataset.value); });
    }

    function path() { return "ws/camera?" + q({ facing, size }); }
    function restart() { if (!paused && document.visibilityState === "visible") video.start(path()); }

    function render() {
      const live = video.state === "live";
      pill.className = "glass" + (live ? " live" : "");
      pill.lastChild.textContent = live ? `Live · ${facing === "front" ? "front" : "back"}` : "Camera";
      let ic = null, title = "Opening the camera…", detail = "";
      if (paused) { ic = "pause"; title = "Paused"; }
      else if (video.state === "unsupported") { ic = "alert"; title = "Can't play here"; detail = video.detail; }
      else if (video.state === "reconnecting") { title = "Reconnecting…"; detail = video.detail || ""; }
      else if (video.state === "waiting") { title = "Waiting for the picture…"; }
      clear(msgIcon, ic ? icon(ic) : h("span.spin", { style: { width: "28px", height: "28px" } }));
      msgTitle.textContent = title;
      msgDetail.textContent = detail;
    }

    let recTimer = 0;
    function renderBtns() {
      clear(pauseBtn, icon(paused ? "play" : "pause"), paused ? "Resume" : "Pause");
      const torchOn = !!store.get("torch.local");      // no torch topic: what this browser last set
      clear(torchBtn, icon("torch"), torchOn ? "Torch off" : "Torch");
      torchBtn.classList.toggle("on", torchOn);
      clear(recBtn, icon(recording ? "stop" : "record"), recording ? "Stop recording" : "Record");
      recBtn.classList.toggle("red", !!recording);
      recPill.classList.toggle("hidden", !recording);
      clearInterval(recTimer);
      if (recording) {
        const tick = () => { recPill.lastChild.textContent = "REC " + fmtDur((Date.now() - recording.since) / 1000); };
        tick();
        recTimer = setInterval(tick, 1000);
      }
      render();
    }

    async function torch(btn) {
      const on = !store.get("torch.local");
      const r = await run("camera.torch", { on }, { btn });
      if (r) { store.set("torch.local", on); renderBtns(); }
    }

    let photoUrl = null;
    async function photo(btn) {
      await busy(btn, async () => {
        let r;
        try { r = await fetch("api/camera/photo?" + q({ facing }), { cache: "no-store" }); } catch (e) { throw new Error("The phone is not reachable right now."); }
        if (!r.ok) {
          let m = `The photo failed (HTTP ${r.status})`;
          try { const d = await r.json(); if (d.error) m = d.error; } catch (e) { /* not JSON */ }
          throw new Error(m);
        }
        const blob = await r.blob();
        if (photoUrl) URL.revokeObjectURL(photoUrl);
        photoUrl = URL.createObjectURL(blob);
        const d = new Date();
        const p = (x) => String(x).padStart(2, "0");
        const name = `photo-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.jpg`;
        clear(photoBox, h("div.card.stack", null,
          h("div.card-title", { style: { margin: 0 } }, icon("image"), "Photo", h("span.actions", null, h("span.muted", null, d.toLocaleTimeString()))),
          h("img", { src: photoUrl, alt: "The photo just taken" }),
          h("a.btn.sm", { href: photoUrl, download: name }, icon("download"), "Download " + name)));
      });
    }

    async function record(btn) {
      if (recording) {
        const r = await run("camera.record", { action: "stop" }, { btn });
        if (r) { recording = null; toast(r.path ? "Saved on the phone: " + r.path : "Recording stopped", "ok"); }
      } else {
        const r = await run("camera.record", { action: "start", facing }, { btn });
        if (r) recording = { since: Date.now(), facing };
      }
      renderBtns();
    }

    const vis = () => { if (document.visibilityState === "visible") restart(); else video.stop(); };
    document.addEventListener("visibilitychange", vis);
    renderBtns();
    (async () => {
      try {
        cams = (await op("camera.list")) || [];
      } catch (e) {
        toast(errText(e), "err");
      }
      if (cams.length && !cams.some((c) => c.facing === facing)) { facing = cams[0].facing; facingSeg.set(facing); }
      pickSize();
      restart();
    })();
    return () => {
      document.removeEventListener("visibilitychange", vis);
      clearInterval(recTimer);
      video.destroy();
      if (photoUrl) URL.revokeObjectURL(photoUrl);
    };
  },
};
