// Remote screen (SCR-01..04): the Pi's desktop, live, controlled from the picture -
// like TeamViewer / UltraViewer. Mouse and keyboard go through the normal input ops.

import { h, clear, icon, store, conn, run, seg, toggle, sheet, formBox, toast } from "../core.js";
import { PreviewPlayer } from "../preview.js";

const PREF = "arstro.screen.control";
const SPECIAL = {
  Enter: "Return", Backspace: "BackSpace", Tab: "Tab", Escape: "Escape", Delete: "Delete", Insert: "Insert",
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right", Home: "Home", End: "End",
  PageUp: "Page_Up", PageDown: "Page_Down", " ": "space", ContextMenu: "Menu", PrintScreen: "Print",
  Meta: "Super_L", OS: "Super_L",
};
const BUTTONS = ["left", "middle", "right"];

function loadPref() {
  try { return localStorage.getItem(PREF) !== "0"; } catch (e) { return true; }
}

export default {
  id: "screen", title: "Screen", icon: "screen",

  mount(root) {
    let st = store.get("screen") || {};
    let control = loadPref();
    let pState = { state: "stopped", detail: "" };

    // ------------------------------------------------------------ picture
    const msgIcon = h("span");
    const msgTitle = h("b");
    const msgDetail = h("small");
    const msg = h("div.msg", null, msgIcon, msgTitle, msgDetail);
    const pill = h("span.glass", null, h("span.dot"), h("span", null, "Screen"));
    const size = h("span.glass.hidden");
    const fsBtn = h("button.btn.icon", { title: "Full screen", "aria-label": "Full screen",
      onclick: (e) => { e.stopPropagation(); document.fullscreenElement ? document.exitFullscreen() : stage.requestFullscreen?.(); } },
      icon("expand"));
    const stage = h("div.preview.screen-stage", { tabindex: "0", role: "application", "aria-label": "Remote screen" },
      msg, h("div.ov.tl", null, pill), h("div.ov.tr", null, size), h("div.ov.br", null, fsBtn));
    const player = new PreviewPlayer(stage, {
      onState: (s, d) => { pState = { state: s, detail: d }; render(); },
      onConfig: () => render(),
      onPlaying: () => render(),
    }, "/ws/screen");

    // ------------------------------------------------------------ toolbar
    const quality = seg([["low", "Low"], ["medium", "Medium"], ["high", "High"]], st.quality || "medium",
      (q) => run("screen.settings.set", { settings: { quality: q } }));
    const controlToggle = toggle(control, (on) => {
      control = on;
      try { localStorage.setItem(PREF, on ? "1" : "0"); } catch (e) { /* private mode */ }
      stage.classList.toggle("control", on);
      if (on) stage.focus();
    }, { label: "Control the Pi" });
    const keysBtn = h("button.btn.sm", { onclick: () => openKeys() }, icon("keyboard"), "Keys");
    const typeBtn = h("button.btn.sm", { onclick: () => typeText() }, icon("send"), "Type text");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Screen"), h("div.sub", null, "The Pi's desktop, live · click the picture to control it"))),
      h("div.screen-bar", null,
        h("label.row", { style: { gap: "10px", cursor: "pointer" } }, controlToggle, h("span.dim", null, "Control")),
        h("div.grow"), keysBtn, typeBtn, quality),
      stage);
    stage.classList.toggle("control", control);

    function render() {
      const scr = st.screen || [];
      const playing = player.playing;
      if (scr[0]) stage.style.aspectRatio = `${scr[0]} / ${scr[1]}`;
      pill.className = "glass" + (playing ? " live" : "");
      pill.lastChild.textContent = playing ? (st.viewers > 1 ? `Live · ${st.viewers} watching` : "Live") : "Screen";
      const cfg = player.config;
      size.classList.toggle("hidden", !scr[0]);
      size.textContent = scr[0] ? `${scr[0]}×${scr[1]}` + (cfg && playing && cfg.width !== scr[0] ? ` → ${cfg.width}×${cfg.height}` : "") : "";
      let ic = null, title = "Connecting to the desktop…", detail = "";
      if (!st.available) { ic = "no-signal"; title = "No desktop"; detail = st.error || "The server has no X display."; }
      else if (st.state === "error" || pState.state === "no-signal") { ic = "no-signal"; title = "No picture"; detail = st.error || pState.detail; }
      else if (pState.state === "unsupported") { ic = "alert"; title = "Can't play here"; detail = pState.detail; }
      clear(msgIcon, ic ? icon(ic) : h("span.spin", { style: { width: "28px", height: "28px" } }));
      msgTitle.textContent = title;
      msgDetail.textContent = detail;
    }

    // ------------------------------------------------------ mouse → the Pi
    /** Map a client point to screen pixels (the picture is letterboxed inside the stage). */
    function toScreen(e) {
      const scr = st.screen;
      if (!scr || !scr[0]) return null;
      const r = stage.getBoundingClientRect();
      const k = Math.min(r.width / scr[0], r.height / scr[1]);
      const pw = scr[0] * k, ph = scr[1] * k;
      const x0 = r.left + (r.width - pw) / 2, y0 = r.top + (r.height - ph) / 2;
      const x = (e.clientX - x0) / k, y = (e.clientY - y0) / k;
      if (x < 0 || y < 0 || x >= scr[0] || y >= scr[1]) return null;
      return [Math.round(x), Math.round(y)];
    }
    let pendingMove = null, lastSent = null, raf = 0, scroll = { dx: 0, dy: 0 };
    const flush = () => {
      raf = 0;
      if (pendingMove && (!lastSent || pendingMove[0] !== lastSent[0] || pendingMove[1] !== lastSent[1])) {
        conn.notify("in.move_to", { x: pendingMove[0], y: pendingMove[1] });
        lastSent = pendingMove;
      }
      pendingMove = null;
      if (scroll.dx || scroll.dy) {
        conn.notify("in.scroll", { dx: Math.round(scroll.dx * 100) / 100, dy: Math.round(scroll.dy * 100) / 100 });
        scroll = { dx: 0, dy: 0 };
      }
    };
    const queue = () => { if (!raf) raf = requestAnimationFrame(flush); };
    const moveNow = (p) => { pendingMove = p; if (raf) { cancelAnimationFrame(raf); raf = 0; } flush(); };

    const touches = new Map();
    const held = new Set();
    stage.addEventListener("pointermove", (e) => {
      if (!control) return;
      if (e.pointerType === "touch" && touches.size >= 2) {           // two fingers: scroll
        const t = touches.get(e.pointerId);
        if (t) { scroll.dy += -(e.clientY - t.y) / 25; scroll.dx += -(e.clientX - t.x) / 25; t.x = e.clientX; t.y = e.clientY; queue(); }
        return;
      }
      const p = toScreen(e);
      if (p) { pendingMove = p; queue(); }
    });
    stage.addEventListener("pointerdown", (e) => {
      if (!control || e.target.closest(".btn")) return;
      stage.focus();
      if (e.pointerType === "touch") {
        touches.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (touches.size >= 2) { releaseAll(); return; }
      }
      const p = toScreen(e);
      if (!p) return;
      e.preventDefault();
      stage.setPointerCapture(e.pointerId);
      moveNow(p);
      const b = BUTTONS[e.button] || "left";
      held.add(b);
      conn.notify("in.btn", { b, a: "down" });
    });
    const up = (e) => {
      touches.delete(e.pointerId);
      if (!control) return;
      const b = BUTTONS[e.button] || "left";
      const p = toScreen(e);
      if (p) moveNow(p);
      if (held.delete(b)) conn.notify("in.btn", { b, a: "up" });
    };
    stage.addEventListener("pointerup", up);
    stage.addEventListener("pointercancel", (e) => { touches.delete(e.pointerId); releaseAll(); });
    stage.addEventListener("wheel", (e) => {
      if (!control) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 1 : e.deltaMode === 2 ? 10 : 1 / 60;
      scroll.dy += e.deltaY * unit;
      scroll.dx += e.deltaX * unit;
      queue();
    }, { passive: false });
    stage.addEventListener("contextmenu", (e) => e.preventDefault());
    function releaseAll() {
      for (const b of held) conn.notify("in.btn", { b, a: "up" });
      held.clear();
    }

    // --------------------------------------------------- keyboard → the Pi
    stage.addEventListener("focus", () => stage.classList.add("focused"));
    stage.addEventListener("blur", () => { stage.classList.remove("focused"); releaseAll(); });
    stage.addEventListener("keydown", (e) => {
      if (!control) return;
      if (["Control", "Alt", "Shift", "AltGraph"].includes(e.key)) return;   // sent with the next key
      e.preventDefault();
      const mods = [e.ctrlKey && "ctrl", e.altKey && "alt", e.metaKey && e.key !== "Meta" && "super"].filter(Boolean);
      if (e.key.length === 1 && !mods.length) return conn.notify("in.text", { s: e.key });
      const k = SPECIAL[e.key] || (/^F\d{1,2}$/.test(e.key) ? e.key : e.key.length === 1 ? e.key.toLowerCase() : null);
      if (!k) return;
      if (e.shiftKey && e.key.length !== 1) mods.push("shift");
      conn.notify("in.key", { k, mods });
    });

    function openKeys() {
      const key = (label, k, mods = []) => h("button.btn", { onclick: () => { conn.notify("in.key", { k, mods }); stage.focus(); } }, label);
      const s = sheet({ title: "Send keys", body: h("div.keys", null,
        key("Esc", "Escape"), key("Tab", "Tab"), key("Enter", "Return"), key("Super", "Super_L"),
        key("Alt+Tab", "Tab", ["alt"]), key("Alt+F4", "F4", ["alt"]), key("Ctrl+Alt+T", "t", ["ctrl", "alt"]),
        key("Ctrl+C", "c", ["ctrl"]), key("Ctrl+V", "v", ["ctrl"]), key("Ctrl+Z", "z", ["ctrl"]),
        key("PrtSc", "Print"), key("F11", "F11")) });
      void s;
    }

    async function typeText() {
      const v = await formBox({ title: "Type on the Pi", ok: "Type", text: "The text is typed at the Pi's cursor.",
        fields: [{ name: "t", label: "Text", placeholder: "Paste or write text here" }] });
      if (v && v.t) { conn.notify("in.text", { s: v.t }); toast("Typed " + v.t.length + " characters", "ok"); }
      stage.focus();
    }

    // ------------------------------------------------------------- wiring
    const sync = () => {
      if (document.visibilityState === "visible" && st.available !== false) player.start(); else player.stop();
      render();
    };
    const offs = [store.on("screen", (v) => { st = v || {}; quality.set(st.quality); render(); })];
    document.addEventListener("visibilitychange", sync);
    sync();
    if (control) setTimeout(() => stage.focus(), 100);
    return () => {
      offs.forEach((f) => f());
      document.removeEventListener("visibilitychange", sync);
      releaseAll();
      if (raf) cancelAnimationFrame(raf);
      player.destroy();
    };
  },
};
