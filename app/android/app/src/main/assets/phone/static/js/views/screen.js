// Screen (PHN-04): the phone's screen live (H.264 over ws/screen, WebCodecs) and remote control
// from the picture: tap, long press (held 500 ms), swipe, scroll (wheel), typing, and the system
// keys. Control messages go over the same socket (fewer requests than ops): {"t": "tap", x, y}
// with x, y in 0..1 of the picture.

import { h, clear, icon, store, op, run, seg, toast, errText, pref, featureReady } from "../core.js";
import { VideoView } from "../video.js";

const NAV = [["back", "Back", "back"], ["home", "Home", "home"], ["recents", "Recents", "recents"]];
const KEYS = [
  ["notifications", "Notifications", "shade"], ["quick_settings", "Quick settings", "quick"],
  ["lock", "Lock", "lock"], ["power_dialog", "Power menu", "power"], ["wake", "Wake", "sun"],
];
const LONG_MS = 500;
const MOVE_PX = 10;

export default {
  mount(root) {
    let st = store.get("screen") || {};
    let consentWait = false;          // screen.start said: waiting for the person on the phone
    let stopped = false;              // "Stop sharing" pressed here
    let startErr = "";

    // ------------------------------------------------------------ picture
    const msgIcon = h("span"), msgTitle = h("b"), msgDetail = h("small"), msgBtn = h("span");
    const msg = h("div.msg", null, msgIcon, msgTitle, msgDetail, msgBtn);
    const pill = h("span.glass", null, h("span.dot"), h("span", null, "Screen"));
    const fsBtn = h("button.btn.icon", { "aria-label": "Full screen", title: "Full screen",
      onclick: (e) => { e.stopPropagation(); document.fullscreenElement ? document.exitFullscreen() : col.requestFullscreen?.().catch(() => {}); } },
      icon("expand"));
    const stage = h("div.stage.screen-stage", { tabindex: "0", role: "application", "aria-label": "The phone's screen" },
      msg, h("div.ov.tl", null, pill), h("div.ov.br", null, fsBtn));
    const video = new VideoView(stage, {
      onState: () => render(),
      onConfig: (cfg) => { fitStage(cfg.width, cfg.height); render(); },
      onMessage: (m) => { if (m.t === "error" && m.error) toast(m.error, "err"); },
    });
    const send = (obj) => video.send(obj);

    // the phone's own navigation bar under the picture (Back, Home, Recents); the stage and the bar
    // go full screen together
    const navBtns = NAV.map(([key, label, ic]) => h("button.btn.ghost", { "data-key": key, "aria-label": label, title: label,
      onclick: () => sendKey(key) }, icon(ic)));
    const col = h("div.screen-col", null, stage, h("div.phone-nav", null, navBtns));
    function fitStage(w, hgt) {
      if (!w || !hgt) { w = 9; hgt = 16; }
      stage.style.aspectRatio = `${w} / ${hgt}`;
      col.style.setProperty("--ar", String(w / hgt));
    }
    fitStage(st.width, st.height);

    // ------------------------------------------------------------ toolbar
    const quality = seg([[480, "480p"], [720, "720p"], [1080, "1080p"]], pref.get("screen.quality", 720), (q) => {
      pref.set("screen.quality", q);
      video.setExtra({ t: "quality", max: q });
    }, { label: "Quality" });
    video.extra = { t: "quality", max: pref.get("screen.quality", 720) };
    const stopBtn = h("button.btn.sm", { onclick: (e) => stopSharing(e.currentTarget), title: "Stop capturing the screen on the phone" },
                      icon("stop"), "Stop sharing");
    const head = h("div.view-head", null,
      h("div", null, h("h1", null, "Screen"), h("div.sub", null, "Tap, hold, swipe or scroll on the picture; click it and type to send keys")),
      h("div.actions", null, quality, stopBtn));

    // ---------------------------------------------------------- side panel
    const keyBtns = KEYS.map(([key, label, ic]) => h("button.btn.sm", { "data-key": key, title: label,
      onclick: () => { sendKey(key); } }, icon(ic), h("span", null, label)));
    const typeInput = h("input.input", { type: "text", placeholder: "Type text to send", "aria-label": "Text to type on the phone",
      autocomplete: "off", spellcheck: "false", onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); typeSend(); } } });
    const typeBtn = h("button.btn.primary", { onclick: () => typeSend(), "aria-label": "Send text" }, icon("send"));
    const controlNote = h("div.note-box.warn.hidden", null, icon("info"),
      h("span", null, "Remote control needs Accessibility for Arstro Remote - allow it in the app on the phone. You can still watch."));
    const side = h("div.screen-side", null,
      h("div.card", null, h("div.card-title", null, "Keys"), h("div.nav-keys", null, keyBtns)),
      h("div.card", null, h("div.card-title", null, icon("keyboard"), "Type"),
        h("div.type-row", null, typeInput, typeBtn),
        h("div.hint", { style: { marginTop: "8px" } }, "Goes into the focused field on the phone. Or click the picture and type; Enter, Backspace and Esc (Back) work too.")),
      controlNote);

    root.append(head, h("div.screen-layout", null, col, side));

    function control() { return st.control !== false; }

    function sendKey(key) {
      if (!send({ t: "key", key })) run("screen.key", { key });
    }
    function typeSend() {
      const t = typeInput.value;
      if (!t) return;
      if (!send({ t: "text", text: t })) run("screen.text", { text: t });
      typeInput.value = "";
      toast(`Typed ${t.length} character${t.length === 1 ? "" : "s"}`, "ok");
    }

    // ------------------------------------------------------------- states
    function render() {
      const playing = video.state === "live";
      pill.className = "glass" + (playing ? " live" : "");
      pill.lastChild.textContent = playing ? (st.watching > 1 ? `Live · ${st.watching} watching` : "Live") : "Screen";
      stage.classList.toggle("nocontrol", !control());
      controlNote.classList.toggle("hidden", control());
      for (const b of [...keyBtns, ...navBtns]) b.disabled = !control();
      typeBtn.disabled = !control();
      let ic = null, title = "Connecting to the phone's screen…", detail = "", btn = null;
      if (stopped) {
        ic = "stop"; title = "Screen sharing stopped"; detail = "The phone no longer captures its screen.";
        btn = h("button.btn.sm", { onclick: () => { stopped = false; begin(); } }, icon("play"), "Share again");
      } else if (startErr) {
        ic = "alert"; title = "The screen did not start"; detail = startErr;
        btn = h("button.btn.sm", { onclick: () => begin() }, icon("refresh"), "Try again");
      } else if (consentWait && !st.consent) {
        ic = "phone"; title = "Waiting for consent on the phone";
        detail = "Someone at the phone must allow screen capture (“Start now”). It is asked once and kept while the app runs.";
        btn = h("button.btn.sm", { onclick: () => begin(true) }, icon("refresh"), "Ask again");
      } else if (video.state === "unsupported") {
        ic = "alert"; title = "Can't play here"; detail = video.detail;
      } else if (video.state === "reconnecting") {
        title = "Reconnecting…"; detail = video.detail || "";
      } else if (video.state === "waiting") {
        title = "Waiting for the picture…"; detail = st.screen_on === false ? "The phone's screen is off - press Wake." : "";
      }
      clear(msgIcon, ic ? icon(ic) : h("span.spin", { style: { width: "28px", height: "28px" } }));
      msgTitle.textContent = title;
      msgDetail.textContent = detail;
      clear(msgBtn, btn);
      stage.classList.toggle("playing", playing && !stopped);
    }

    async function begin(ask = false) {
      startErr = "";
      if (!featureReady("screen")) return;
      if (!ask && st.consent) { consentWait = false; open(); render(); return; }
      render();
      try {
        const r = await op("screen.start");
        if (r && r.consent) { consentWait = false; st = { ...st, consent: true }; open(); }
        else consentWait = true;
      } catch (e) {
        startErr = errText(e);
      }
      render();
    }

    function open() {
      if (document.visibilityState !== "visible" || stopped) return;
      video.start("ws/screen");
    }

    async function stopSharing(btn) {
      video.stop();
      stopped = true;
      render();
      await run("screen.stop", {}, { btn, ok: "Screen sharing stopped" });
    }

    // ------------------------------------------------------ touch → phone
    let down = null;
    const dot = (cx, cy, long) => {
      const r = stage.getBoundingClientRect();
      const d = h("span.touch-dot", { class: long ? "long" : "", style: { left: cx - r.left + "px", top: cy - r.top + "px" } });
      stage.append(d);
      setTimeout(() => d.remove(), 500);
    };
    stage.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".btn") || (e.pointerType === "mouse" && e.button !== 0)) return;
      stage.focus({ preventScroll: true });
      if (!control() || down) return;
      const p = video.toPicture(e.clientX, e.clientY);
      if (!p) return;
      e.preventDefault();
      try { stage.setPointerCapture(e.pointerId); } catch (x) { /* synthetic */ }
      const d = { id: e.pointerId, p, cx: e.clientX, cy: e.clientY, t: performance.now(), moved: false, long: false };
      d.timer = setTimeout(() => {
        if (down !== d || d.moved) return;
        d.long = true;
        send({ t: "long", x: p[0], y: p[1] });
        dot(d.cx, d.cy, true);
      }, LONG_MS);
      down = d;
    });
    stage.addEventListener("pointermove", (e) => {
      if (!down || e.pointerId !== down.id) return;
      if (!down.moved && Math.hypot(e.clientX - down.cx, e.clientY - down.cy) > MOVE_PX) { down.moved = true; clearTimeout(down.timer); }
    });
    stage.addEventListener("pointerup", (e) => {
      if (!down || e.pointerId !== down.id) return;
      const d = down;
      down = null;
      clearTimeout(d.timer);
      if (d.long) return;
      if (d.moved && Math.hypot(e.clientX - d.cx, e.clientY - d.cy) > MOVE_PX) {
        const q = video.toPicture(e.clientX, e.clientY, true);
        if (!q) return;
        const ms = Math.round(Math.max(80, Math.min(3000, performance.now() - d.t)));
        send({ t: "swipe", x1: d.p[0], y1: d.p[1], x2: q[0], y2: q[1], ms });
      } else {
        send({ t: "tap", x: d.p[0], y: d.p[1] });
        dot(d.cx, d.cy, false);
      }
    });
    stage.addEventListener("pointercancel", () => { if (down) clearTimeout(down.timer); down = null; });
    stage.addEventListener("contextmenu", (e) => e.preventDefault());

    let wheel = null;
    stage.addEventListener("wheel", (e) => {
      if (!control()) return;
      const p = video.toPicture(e.clientX, e.clientY);
      const pr = video.pictureRect();
      if (!p || !pr) return;
      e.preventDefault();
      const px = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * pr.height : e.deltaY;
      if (!wheel) {
        wheel = { x: p[0], y: p[1], dy: 0 };
        setTimeout(() => {
          const w = wheel;
          wheel = null;
          const dy = Math.round(Math.max(-2, Math.min(2, w.dy)) * 1000) / 1000;
          if (dy) send({ t: "scroll", x: w.x, y: w.y, dy });
        }, 120);
      }
      wheel.dy += (px / pr.height) * 1.5;
    }, { passive: false });

    // --------------------------------------------------- keyboard → phone
    let pending = "", flushT = 0;
    const flushText = () => { clearTimeout(flushT); flushT = 0; if (pending) { send({ t: "text", text: pending }); pending = ""; } };
    stage.addEventListener("focus", () => stage.classList.add("focused"));
    stage.addEventListener("blur", () => { stage.classList.remove("focused"); flushText(); });
    stage.addEventListener("keydown", (e) => {
      if (!control()) return;
      const altGr = e.getModifierState && e.getModifierState("AltGraph");      // AltGr+key types a character
      if ((e.ctrlKey || e.metaKey || e.altKey) && !altGr) return;            // paste (Ctrl+V) comes as a paste event
      if (e.key === "Enter") { e.preventDefault(); flushText(); send({ t: "key", key: "enter" }); return; }
      if (e.key === "Backspace") { e.preventDefault(); flushText(); send({ t: "key", key: "backspace" }); return; }
      if (e.key === "Escape") { if (!document.fullscreenElement) { e.preventDefault(); flushText(); send({ t: "key", key: "back" }); } return; }
      if (e.key.length === 1) {
        e.preventDefault();
        pending += e.key;
        if (!flushT) flushT = setTimeout(flushText, 60);
      }
    });
    stage.addEventListener("paste", (e) => {
      if (!control()) return;
      const t = e.clipboardData && e.clipboardData.getData("text");
      if (t) { e.preventDefault(); flushText(); send({ t: "text", text: t }); toast(`Pasted ${t.length} characters`, "ok"); }
    });

    const onFs = () => {
      const fs = document.fullscreenElement === col;
      clear(fsBtn, icon(fs ? "shrink" : "expand"));
    };
    document.addEventListener("fullscreenchange", onFs);

    // ------------------------------------------------------------- wiring
    const vis = () => {
      if (document.visibilityState === "visible") { if (!stopped && (st.consent || !consentWait)) begin(); }
      else video.stop();
    };
    const offs = [store.on("screen", (v) => {
      const had = st.consent;
      st = v || {};
      if (!video.config) fitStage(st.width, st.height);
      if (st.consent && !had && consentWait && !stopped) { consentWait = false; open(); }
      render();
    }, { now: false })];
    document.addEventListener("visibilitychange", vis);
    window.phoneDebug.screen = { video, send };
    render();
    begin();
    return () => {
      offs.forEach((f) => f());
      document.removeEventListener("visibilitychange", vis);
      document.removeEventListener("fullscreenchange", onFs);
      flushText();
      if (down) clearTimeout(down.timer);
      video.destroy();
    };
  },
};
