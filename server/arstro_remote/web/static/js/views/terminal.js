// Terminal (TERM-01..04): the Pi's shared shells. Every controller sees the same list and
// can attach to any shell; output is replayed exactly from the last byte we have.

import { h, clear, icon, store, conn, toast, confirmBox, errText, loadScript, loadStyle, isNarrow, CONTROLLER_NAMES } from "../core.js";

const THEME = {
  background: "#0c0d10", foreground: "#e3e6ea", cursor: "#7c9cff", cursorAccent: "#0c0d10",
  selectionBackground: "rgba(124,156,255,.3)",
  black: "#1b1f25", red: "#ff6b6b", green: "#3ddc97", yellow: "#f5b841", blue: "#7c9cff", magenta: "#c49bff",
  cyan: "#5fd4e6", white: "#d7dbe0", brightBlack: "#5c6370", brightRed: "#ff8a8a", brightGreen: "#6ee7b7",
  brightYellow: "#fcd34d", brightBlue: "#a5b8ff", brightMagenta: "#d8b4fe", brightCyan: "#99f6e4", brightWhite: "#ffffff",
};

// Shells we are attached to survive view switches (the server keeps streaming to us).
const mgr = {
  terms: new Map(),        // id -> {xterm, fit, received, exited, el}
  active: null,
  wired: false,
  ctrl: false,
  onChange: null,
};

async function loadXterm() {
  loadStyle("/assets/vendor/xterm.css");
  await loadScript("/assets/vendor/xterm.js");
  await loadScript("/assets/vendor/addon-fit.js");
}

function wire() {
  if (mgr.wired) return;
  mgr.wired = true;
  conn.on("term", (id, offset, data) => {
    const t = mgr.terms.get(id);
    if (!t) return;
    const end = offset + data.length;
    if (end <= t.received) return;                       // already have it
    if (offset < t.received) data = data.subarray(t.received - offset);
    t.received = end;
    t.xterm.write(data);
  });
  conn.on("event", (ev, msg) => {
    if (ev !== "term.exit") return;
    const t = mgr.terms.get(msg.term);
    if (!t) return;
    t.exited = true;
    t.xterm.write(`\r\n\x1b[2m[shell ${msg.reason === "closed" ? "closed" : "exited" + (msg.code !== null && msg.code !== undefined ? " with code " + msg.code : "")}]\x1b[0m\r\n`);
    setTimeout(() => drop(msg.term), 1200);
  });
  conn.on("ready", async () => {            // reconnected: resume every shell exactly
    for (const [id, t] of mgr.terms) {
      try {
        const r = await conn.call("term.attach", { term: id, since: t.received, cols: t.xterm.cols, rows: t.xterm.rows });
        if (r.gap) { t.xterm.reset(); t.received = r.start; }
      } catch (e) {
        drop(id);
      }
    }
  });
}

function makeXterm(id) {
  const xterm = new window.Terminal({
    theme: THEME, fontFamily: 'ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: isNarrow() ? 12 : 13.5, lineHeight: 1.15, cursorBlink: true, scrollback: 5000, allowProposedApi: false,
    macOptionIsMeta: true,
  });
  const fit = new window.FitAddon.FitAddon();
  xterm.loadAddon(fit);
  const el = h("div.term-host");
  xterm.onData((d) => {
    if (mgr.ctrl && d.length === 1) {
      const c = d.toLowerCase().charCodeAt(0);
      if (c >= 97 && c <= 122) d = String.fromCharCode(c - 96);
      mgr.ctrl = false;
      mgr.onChange && mgr.onChange();
    }
    conn.termWrite(id, d);
  });
  const t = { xterm, fit, el, received: 0, exited: false, opened: false, size: [0, 0] };
  mgr.terms.set(id, t);
  return t;
}

function drop(id) {
  const t = mgr.terms.get(id);
  if (!t) return;
  mgr.terms.delete(id);
  t.xterm.dispose();
  t.el.remove();
  if (mgr.active === id) mgr.active = null;
  mgr.onChange && mgr.onChange();
}

export default {
  id: "terminal", title: "Terminal", icon: "terminal",

  /** opts.kind "serial": the serial consoles of IO › UART (opts.onNew opens a port). */
  mount(root, opts = {}) {
    const kind = opts.kind || "shell";
    const serial = kind === "serial";
    const tabs = h("div.term-tabs");
    const box = h("div.term-box");
    const keys = h("div.term-keys");
    const wrap = h("div.term-wrap", { class: serial ? "embedded" : "" }, tabs, box, keys);
    if (!serial) root.append(h("div.view-head", null, h("div", null, h("h1", null, "Terminal"),
                  h("div.sub", null, "Shared shells on the Pi · the app and the CLI see the same ones"))));
    root.append(wrap);
    let ready = false;
    const mine = (v) => (v || []).filter((t) => (t.kind || "shell") === kind);
    let list = mine(store.get("terminals"));
    const label = (t) => serial ? `${t.port || "serial"} · ${(t.settings || {}).baud || ""}` : `Shell ${t.term}`;
    const newLabel = serial ? "Open a port" : "New shell";
    const newAction = () => (serial ? opts.onNew && opts.onNew() : openShell());

    const sendKey = (seq) => { if (mgr.active !== null) { conn.termWrite(mgr.active, seq); mgr.terms.get(mgr.active)?.xterm.focus(); } };
    const renderKeys = () => clear(keys,
      [["Esc", "\x1b"], ["Tab", "\t"]].map(([l, s]) => h("button.btn", { onclick: () => sendKey(s) }, l)),
      h("button.btn", { class: mgr.ctrl ? "on" : "", onclick: () => { mgr.ctrl = !mgr.ctrl; renderKeys(); mgr.terms.get(mgr.active)?.xterm.focus(); } }, "Ctrl"),
      [["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"], ["^C", "\x03"], ["^D", "\x04"],
       ["|", "|"], ["~", "~"], ["/", "/"], ["-", "-"], ["Home", "\x1b[H"], ["End", "\x1b[F"], ["PgUp", "\x1b[5~"], ["PgDn", "\x1b[6~"]]
        .map(([l, s]) => h("button.btn", { onclick: () => sendKey(s) }, l)));
    mgr.onChange = () => { renderTabs(); renderKeys(); };

    function renderTabs() {
      const ids = new Set(list.map((t) => t.term));
      clear(tabs,
        list.map((t) => {
          return h("button.term-tab", { class: mgr.active === t.term ? "active" : "", onclick: () => show(t.term),
                                        title: `opened by ${CONTROLLER_NAMES[t.opened_by] || t.opened_by || "?"}` },
            icon(serial ? "plug" : "cmd"), label(t),
            h("span.v", null, t.viewers ? `${t.viewers} viewing` : "idle"),
            h("span.x", { role: "button", "aria-label": serial ? "Close port" : "Close shell", title: serial ? "Close port" : "Close shell",
                          onclick: (e) => { e.stopPropagation(); closeShell(t); } }, icon("x")));
        }),
        h("button.btn.sm", { onclick: () => newAction() }, icon("plus"), newLabel));
      if (!list.length) {
        clear(box, h("div.empty", { style: { paddingTop: "80px" } }, icon(serial ? "plug" : "terminal"),
          h("div", null, serial ? "No serial console open." : "No shells open."),
          h("div", { style: { marginTop: "14px" } }, h("button.btn.primary", { onclick: () => newAction() }, icon("plus"),
            serial ? "Open a serial port" : "Open a shell"))));
      } else if (mgr.active === null || !ids.has(mgr.active)) {
        const first = list.find((t) => mgr.terms.has(t.term)) || list[0];
        if (ready) show(first.term);
      }
    }

    function fitActive() {
      const t = mgr.terms.get(mgr.active);
      if (!t || !t.el.isConnected || !t.el.offsetWidth) return;
      try { t.fit.fit(); } catch (e) { return; }
      const size = [t.xterm.cols, t.xterm.rows];
      if (size[0] !== t.size[0] || size[1] !== t.size[1]) {
        t.size = size;
        conn.call("term.resize", { term: mgr.active, cols: size[0], rows: size[1] }).catch(() => {});
      }
    }

    async function show(id) {
      if (!ready) return;
      let t = mgr.terms.get(id);
      if (!t) {
        t = makeXterm(id);
        try {
          const r = await conn.call("term.attach", { term: id, since: 0 });
          t.received = r.start;
        } catch (e) {
          drop(id);
          toast(errText(e), "err");
          return;
        }
      }
      mgr.active = id;
      clear(box, t.el);
      if (!t.opened) { t.xterm.open(t.el); t.opened = true; }
      requestAnimationFrame(() => { fitActive(); t.xterm.focus(); });
      renderTabs();
    }

    async function openShell() {
      if (!ready) return;
      try {
        const cols = 100, rows = 30;
        const r = await conn.call("term.open", { cols, rows });
        const t = makeXterm(r.term);
        t.received = 0;
        mgr.active = r.term;
        list = store.get("terminals") || list;
        if (!list.some((x) => x.term === r.term)) list = [...list, { term: r.term, viewers: 1 }];
        renderTabs();
        show(r.term);
      } catch (e) {
        toast(errText(e), "err");
      }
    }

    async function closeShell(t) {
      if (t.viewers > (mgr.terms.has(t.term) ? 1 : 0)) {
        const ok = await confirmBox({ title: `Close ${serial ? t.port : "shell " + t.term}?`, ok: "Close", danger: true,
          text: serial ? "Another controller is watching this port. Closing it ends the console for everyone."
                       : "Another controller is using this shell. Closing ends its programs for everyone." });
        if (!ok) return;
      }
      conn.call("term.close", { term: t.term }).catch((e) => toast(errText(e), "err"));
    }

    const ro = new ResizeObserver(() => fitActive());
    ro.observe(box);
    const offs = [store.on("terminals", (v) => { list = mine(v); renderTabs(); })];
    renderKeys();
    loadXterm().then(() => {
      wire();
      ready = true;
      const want = opts.term !== undefined ? opts.term : mgr.active;
      if (want !== null && list.some((t) => t.term === want)) show(want);
      else renderTabs();
    }).catch((e) => toast(errText(e), "err"));
    root.showTerm = (id) => { if (ready) show(id); else opts.term = id; };

    return () => {
      offs.forEach((f) => f());
      ro.disconnect();
      mgr.onChange = null;
      for (const t of mgr.terms.values()) t.el.remove();
    };
  },
};
