// Remote (INP-01..04): touchpad (moves only), press-and-hold buttons, drag lock, scroll strip,
// two-finger scroll, keyboard keys / text, and optional mouse + keyboard capture on desktops.

import { h, clear, icon, conn, toggle, isNarrow } from "../core.js";

const PREF = "arstro.remote.sens";
const SPECIAL = {
  Enter: "Return", Backspace: "BackSpace", Tab: "Tab", Escape: "Escape", Delete: "Delete", Insert: "Insert",
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right", Home: "Home", End: "End",
  PageUp: "Page_Up", PageDown: "Page_Down", " ": "space", ContextMenu: "Menu", PrintScreen: "Print",
};

function loadSens() {
  try { return Number(localStorage.getItem(PREF)) || 1.4; } catch (e) { return 1.4; }
}

export default {
  id: "remote", title: "Remote", icon: "remote",

  mount(root) {
    let sens = loadSens();
    const mods = new Set();
    const held = new Set();
    let pending = { dx: 0, dy: 0, sx: 0, sy: 0 };
    let raf = 0;
    let capture = false;

    const flush = () => {
      raf = 0;
      if (pending.dx || pending.dy) conn.notify("in.move", { dx: round(pending.dx), dy: round(pending.dy) });
      if (pending.sx || pending.sy) conn.notify("in.scroll", { dx: round(pending.sx), dy: round(pending.sy) });
      pending = { dx: 0, dy: 0, sx: 0, sy: 0 };
    };
    const queue = () => { if (!raf) raf = requestAnimationFrame(flush); };
    const round = (v) => Math.round(v * 100) / 100;
    const accel = (d) => sens * (1 + Math.min(1.6, Math.abs(d) / 18));

    // --------------------------------------------------------------- pad
    const dot = h("div.touch");
    const padHint = h("div.hint", null, isNarrow() ? "Slide to move · two fingers to scroll" : "Drag to move · wheel or two fingers to scroll");
    const pad = h("div.pad", { role: "application", "aria-label": "Touchpad" }, dot, padHint);
    const points = new Map();

    pad.addEventListener("pointerdown", (e) => {
      if (capture) return;
      pad.setPointerCapture(e.pointerId);
      points.set(e.pointerId, { x: e.clientX, y: e.clientY });
      pad.classList.add("active");
      showDot(e);
    });
    pad.addEventListener("pointermove", (e) => {
      if (capture) {
        pending.dx += e.movementX * sens * 0.8;
        pending.dy += e.movementY * sens * 0.8;
        return queue();
      }
      const p = points.get(e.pointerId);
      if (!p) return;
      const dx = e.clientX - p.x, dy = e.clientY - p.y;
      p.x = e.clientX; p.y = e.clientY;
      if (points.size >= 2) {                     // two fingers: scroll (natural direction)
        pending.sy += -dy / 22 / points.size;
        pending.sx += -dx / 22 / points.size;
      } else {
        pending.dx += dx * accel(dx);
        pending.dy += dy * accel(dy);
      }
      showDot(e);
      queue();
    });
    const up = (e) => {
      points.delete(e.pointerId);
      if (!points.size) { pad.classList.remove("active"); dot.classList.remove("on"); }
    };
    pad.addEventListener("pointerup", up);
    pad.addEventListener("pointercancel", up);
    pad.addEventListener("wheel", (e) => {
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 1 : e.deltaMode === 2 ? 10 : 1 / 60;
      pending.sy += e.deltaY * unit;
      pending.sx += e.deltaX * unit;
      queue();
    }, { passive: false });
    pad.addEventListener("contextmenu", (e) => e.preventDefault());

    function showDot(e) {
      const r = pad.getBoundingClientRect();
      dot.style.left = e.clientX - r.left + "px";
      dot.style.top = e.clientY - r.top + "px";
      dot.classList.add("on");
    }

    // scroll strip
    const strip = h("div.scroll-strip", { "aria-label": "Scroll" }, icon("up"), icon("down"));
    let stripY = null;
    strip.addEventListener("pointerdown", (e) => { strip.setPointerCapture(e.pointerId); stripY = e.clientY; });
    strip.addEventListener("pointermove", (e) => {
      if (stripY === null) return;
      pending.sy += (e.clientY - stripY) / 16;
      stripY = e.clientY;
      queue();
    });
    strip.addEventListener("pointerup", () => { stripY = null; });
    strip.addEventListener("pointercancel", () => { stripY = null; });

    // ----------------------------------------------------------- buttons
    const btn = (name, label) => {
      const el = h("button.btn", { "aria-label": label + " button" }, label);
      const down = (e) => {
        e.preventDefault();
        el.setPointerCapture?.(e.pointerId);
        if (lock.name === name) return;
        el.classList.add("held");
        held.add(name);
        conn.notify("in.btn", { b: name, a: "down" });
      };
      const release = () => {
        if (!held.has(name) || lock.name === name) return;
        held.delete(name);
        el.classList.remove("held");
        conn.notify("in.btn", { b: name, a: "up" });
      };
      el.addEventListener("pointerdown", down);
      el.addEventListener("pointerup", release);
      el.addEventListener("pointercancel", release);
      el.addEventListener("contextmenu", (e) => e.preventDefault());
      el.dataset.b = name;
      return el;
    };
    const lock = { name: null };
    const left = btn("left", "Left"), middle = btn("middle", "Mid"), right = btn("right", "Right");
    const lockBtn = h("button.btn.sm", { onclick: () => toggleLock() }, icon("lock"), "Drag lock");
    function toggleLock() {
      if (lock.name) {
        lock.name = null;
        held.delete("left");
        left.classList.remove("held");
        lockBtn.classList.remove("primary");
        conn.notify("in.btn", { b: "left", a: "up" });
      } else {
        lock.name = "left";
        held.add("left");
        left.classList.add("held");
        lockBtn.classList.add("primary");
        conn.notify("in.btn", { b: "left", a: "down" });
      }
    }

    // ---------------------------------------------------------- keyboard
    const modBtns = {};
    const renderMods = () => Object.entries(modBtns).forEach(([m, el]) => el.classList.toggle("on", mods.has(m)));
    const sendKey = (k) => {
      conn.notify("in.key", { k, mods: [...mods] });
      if (mods.size) { mods.clear(); renderMods(); }
    };
    const key = (label, k, cls) => h("button.btn", { class: cls || "", onclick: () => sendKey(k) }, label);
    for (const [m, label] of [["ctrl", "Ctrl"], ["alt", "Alt"], ["shift", "Shift"], ["super", "Super"]]) {
      modBtns[m] = h("button.btn", { onclick: () => { mods.has(m) ? mods.delete(m) : mods.add(m); renderMods(); } }, label);
    }
    const text = h("input.input", { placeholder: "Type text for the Pi…", autocomplete: "off", spellcheck: "false",
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); sendText(); } } });
    const sendText = () => {
      if (!text.value) return sendKey("Return");
      conn.notify("in.text", { s: text.value });
      text.value = "";
    };
    const shortcuts = [["Copy", "c", ["ctrl"]], ["Paste", "v", ["ctrl"]], ["Undo", "z", ["ctrl"]], ["All", "a", ["ctrl"]],
                       ["Alt+Tab", "Tab", ["alt"]], ["Close", "F4", ["alt"]]];

    const sensVal = h("span.num.muted", null, sens.toFixed(1) + "×");
    const sensInput = h("input", { type: "range", min: "0.4", max: "4", step: "0.1", value: String(sens),
      oninput: (e) => { sens = Number(e.target.value); sensVal.textContent = sens.toFixed(1) + "×";
                        try { localStorage.setItem(PREF, String(sens)); } catch (x) { /* ignore */ } } });

    const captureToggle = toggle(false, (on) => setCapture(on), { label: "Capture mouse and keyboard" });
    const unavailable = h("div.card.hidden", { style: { marginBottom: "16px", borderColor: "rgba(245,184,65,.4)" } });

    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Remote"), h("div.sub", null, "Mouse and keyboard of the Pi's desktop"))),
      unavailable,
      h("div.remote", null,
        h("div", null,
          h("div.pad-wrap", null, pad, strip),
          h("div.mouse-btns", null, left, middle, right),
          h("div.row.wrap", { style: { marginTop: "12px" } }, lockBtn,
            h("div.row.grow", { style: { minWidth: "200px" } }, h("span.muted", { style: { fontSize: "13px" } }, "Speed"), sensInput, sensVal))),
        h("div.stack", null,
          !isNarrow() ? h("div.card", null,
            h("div.row", null, h("div.grow", null, h("b", null, "Capture mode"),
              h("div.capture-note", null, "Your mouse and keyboard control the Pi directly. Press Esc to release.")), captureToggle)) : null,
          h("div.card", null,
            h("div.card-title", null, icon("keyboard"), "Keyboard"),
            h("div.row", { style: { marginBottom: "12px" } }, text,
              h("button.btn.icon", { "aria-label": "Send text", onclick: sendText }, icon("send"))),
            h("div.keys", { style: { marginBottom: "10px" } }, Object.values(modBtns)),
            h("div.keys", null,
              key("Esc", "Escape"), key("Tab", "Tab"), key("Enter", "Return"), key("⌫", "BackSpace"), key("Del", "Delete"),
              key("Space", "space"), key("↑", "Up"), key("↓", "Down"), key("←", "Left"), key("→", "Right"),
              key("Home", "Home"), key("End", "End"), key("PgUp", "Page_Up"), key("PgDn", "Page_Down"),
              key("Ins", "Insert"), key("Menu", "Menu")),
            h("div.set-group-title", null, "Shortcuts"),
            h("div.keys", null, shortcuts.map(([l, k, m]) => h("button.btn", { onclick: () => conn.notify("in.key", { k, mods: m }) }, l))),
            h("div.set-group-title", null, "Function & media"),
            h("div.keys", null,
              Array.from({ length: 12 }, (_, i) => key("F" + (i + 1), "F" + (i + 1))),
              key("Vol−", "volumedown"), key("Vol+", "volumeup"), key("Mute", "mute"), key("Play", "playpause"))))));

    // ----------------------------------------------------------- capture
    function setCapture(on) {
      if (on) {
        pad.requestPointerLock?.();
      } else if (document.pointerLockElement === pad) {
        document.exitPointerLock();
      }
    }
    const onLock = () => {
      capture = document.pointerLockElement === pad;
      captureToggle.set(capture);
      pad.classList.toggle("active", capture);
      padHint.textContent = capture ? "Capturing · press Esc to release" : "Drag to move · wheel or two fingers to scroll";
    };
    const onMouse = (down) => (e) => {
      if (!capture) return;
      const b = ["left", "middle", "right"][e.button];
      if (b) conn.notify("in.btn", { b, a: down ? "down" : "up" });
    };
    const onDown = onMouse(true), onUp = onMouse(false);
    const onKey = (e) => {
      if (!capture) return;
      if (e.key === "Escape") return;                  // the browser releases the lock
      e.preventDefault();
      const m = [e.ctrlKey && "ctrl", e.altKey && "alt", e.metaKey && "super"].filter(Boolean);
      if (e.key.length === 1 && !m.length) return conn.notify("in.text", { s: e.key });
      const k = SPECIAL[e.key] || (/^F\d{1,2}$/.test(e.key) ? e.key : e.key.length === 1 ? e.key.toLowerCase() : null);
      if (!k) return;
      if (e.shiftKey && e.key.length !== 1) m.push("shift");
      conn.notify("in.key", { k, mods: m });
    };
    document.addEventListener("pointerlockchange", onLock);
    pad.addEventListener("mousedown", onDown);
    pad.addEventListener("mouseup", onUp);
    document.addEventListener("keydown", onKey);

    const input = conn.hello && conn.hello.input;
    if (input && !input.available) {
      unavailable.classList.remove("hidden");
      clear(unavailable, h("div.row", null, icon("alert"), h("span", null,
        "Mouse and keyboard are unavailable: the Pi has no desktop session right now (" + (input.backend || "none") + ").")));
    }

    return () => {
      if (lock.name) toggleLock();
      for (const b of held) conn.notify("in.btn", { b, a: "up" });
      if (document.pointerLockElement === pad) document.exitPointerLock();
      document.removeEventListener("pointerlockchange", onLock);
      document.removeEventListener("keydown", onKey);
      if (raf) cancelAnimationFrame(raf);
    };
  },
};

