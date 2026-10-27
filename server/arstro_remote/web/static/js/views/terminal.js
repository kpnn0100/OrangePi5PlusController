// Terminal (TERM-01..05, TERM-08): the Pi's shared shells. Every controller sees the same list and
// can attach to any shell; output is replayed exactly from the last byte we have. Full screen
// (TERM-05): the shell takes the whole screen; F11 or the corner button leaves it.
//
// Tabs and split panes (TERM-08, like Tilix): this page lays the shells out in tabs, and a tab
// is a tree of panes split right or down, the dividers dragged to resize. The shells are the
// server's and shared; the layout is this page's own (another browser lays them out its way),
// so a shell opened elsewhere arrives here as a tab of its own. The tab strip stays in full
// screen, with + (new tab) and the split buttons. Keys (as Tilix): Ctrl+Alt+R split right,
// Ctrl+Alt+D split down, Ctrl+Shift+T new tab, Ctrl+Shift+W close the pane's shell,
// Ctrl+Shift+X maximize the pane, Alt+arrows move between panes.

import { h, clear, icon, store, conn, toast, confirmBox, errText, loadScript, loadStyle, isNarrow, CONTROLLER_NAMES } from "../core.js";

const THEME = {
  background: "#0c0d10", foreground: "#e3e6ea", cursor: "#7c9cff", cursorAccent: "#0c0d10",
  selectionBackground: "rgba(124,156,255,.3)",
  black: "#1b1f25", red: "#ff6b6b", green: "#3ddc97", yellow: "#f5b841", blue: "#7c9cff", magenta: "#c49bff",
  cyan: "#5fd4e6", white: "#d7dbe0", brightBlack: "#5c6370", brightRed: "#ff8a8a", brightGreen: "#6ee7b7",
  brightYellow: "#fcd34d", brightBlue: "#a5b8ff", brightMagenta: "#d8b4fe", brightCyan: "#99f6e4", brightWhite: "#ffffff",
};

// Shells we are attached to survive view switches (the server keeps streaming to us), and so
// does this page's layout of them.
const mgr = {
  terms: new Map(),        // id -> {xterm, fit, received, exited, el, pane}
  active: null,            // the focused shell (the key row and the shortcuts act on it)
  wired: false,
  ctrl: false,
  onChange: null,
  layouts: {},             // kind -> {tabs: [{key, root, zoom}], current: key}
};
let tabSeq = 1;

// ---------------------------------------------------------------- the layout tree
// a leaf is {t: shell id}; a split is {dir: "row" (side by side) | "col" (stacked), r: 0..1, a, b}
const leaves = (n) => (!n ? [] : n.t !== undefined ? [n.t] : [...leaves(n.a), ...leaves(n.b)]);
function without(n, id) {
  if (!n) return null;
  if (n.t !== undefined) return n.t === id ? null : n;
  const a = without(n.a, id), b = without(n.b, id);
  return a && b ? { ...n, a, b } : a || b;
}
function replaceLeaf(n, id, f) {
  if (!n) return n;
  if (n.t !== undefined) return n.t === id ? f(n) : n;
  return { ...n, a: replaceLeaf(n.a, id, f), b: replaceLeaf(n.b, id, f) };
}

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
    const L = mgr.layouts[kind] || (mgr.layouts[kind] = { tabs: [], current: null });
    const tabs = h("div.term-tabs");
    const box = h("div.term-box");
    const keys = h("div.term-keys");
    const wrap = h("div.term-wrap", { class: serial ? "embedded" : "" }, tabs, box, keys);
    let full = false;
    if (!serial) root.append(h("div.view-head", null, h("div", null, h("h1", null, "Terminal"),
                  h("div.sub", null, "Shared shells on the Pi · the app and the CLI see the same ones"))));
    root.append(wrap);
    let ready = false;
    const mine = (v) => (v || []).filter((t) => (t.kind || "shell") === kind);
    let list = mine(store.get("terminals"));
    const info = (id) => list.find((t) => t.term === id);
    const label = (t) => serial ? `${t.port || "serial"} · ${(t.settings || {}).baud || ""}` : `Shell ${t.term}`;
    const nameOf = (id) => (info(id) ? label(info(id)) : serial ? "serial" : `Shell ${id}`);
    const newLabel = serial ? "Open a port" : "New shell";
    const tabOf = (id) => L.tabs.find((tb) => leaves(tb.root).includes(id));
    const current = () => L.tabs.find((tb) => tb.key === L.current) || null;

    const sendKey = (seq) => { if (mgr.active !== null) { conn.termWrite(mgr.active, seq); mgr.terms.get(mgr.active)?.xterm.focus(); } };
    const renderKeys = () => clear(keys,
      [["Esc", "\x1b"], ["Tab", "\t"]].map(([l, s]) => h("button.btn", { onclick: () => sendKey(s) }, l)),
      h("button.btn", { class: mgr.ctrl ? "on" : "", onclick: () => { mgr.ctrl = !mgr.ctrl; renderKeys(); mgr.terms.get(mgr.active)?.xterm.focus(); } }, "Ctrl"),
      [["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"], ["^C", "\x03"], ["^D", "\x04"],
       ["|", "|"], ["~", "~"], ["/", "/"], ["-", "-"], ["Home", "\x1b[H"], ["End", "\x1b[F"], ["PgUp", "\x1b[5~"], ["PgDn", "\x1b[6~"]]
        .map(([l, s]) => h("button.btn", { onclick: () => sendKey(s) }, l)));
    mgr.onChange = () => { reconcile(); render(); renderKeys(); };

    // ------------------------------------------------------------ layout bookkeeping
    /** Fit the layout to the server's list: a shell that is gone leaves its pane, an empty tab
     *  goes, a shell no tab holds (opened here or by another controller) gets a tab of its own. */
    function reconcile() {
      const ids = new Set(list.map((t) => t.term));
      for (const tb of L.tabs) {
        for (const id of leaves(tb.root)) if (!ids.has(id)) tb.root = without(tb.root, id);
        if (tb.zoom !== undefined && !leaves(tb.root).includes(tb.zoom)) tb.zoom = undefined;
      }
      L.tabs = L.tabs.filter((tb) => tb.root);
      for (const t of list) if (!tabOf(t.term)) L.tabs.push({ key: tabSeq++, root: { t: t.term } });
      if (!current()) L.current = L.tabs.length ? L.tabs[0].key : null;
      const tb = current();
      if (tb && (mgr.active === null || !leaves(tb.root).includes(mgr.active))) mgr.active = leaves(tb.root)[0];
    }
    /** Put shell `id` beside the focused pane (dir "row" = right, "col" = down), or in a new tab. */
    function place(id, dir) {
      for (const tb of L.tabs) tb.root = without(tb.root, id);
      L.tabs = L.tabs.filter((tb) => tb.root);
      const tb = current(), at = mgr.active;
      if (dir && tb && at !== null && leaves(tb.root).includes(at)) {
        tb.root = replaceLeaf(tb.root, at, (leaf) => ({ dir, r: 0.5, a: leaf, b: { t: id } }));
        tb.zoom = undefined;
      } else {
        const nt = { key: tabSeq++, root: { t: id } };
        L.tabs.push(nt);
        L.current = nt.key;
      }
      mgr.active = id;
    }

    // ------------------------------------------------------------ the tab strip
    function renderTabs() {
      const tb = current();
      const panes = tb ? leaves(tb.root).length : 0;
      clear(tabs,
        L.tabs.map((t) => {
          const ids = leaves(t.root);
          const first = info(ids[0]) || { term: ids[0] };
          const viewers = Math.max(0, ...ids.map((id) => (info(id) || {}).viewers || 0));
          return h("button.term-tab", { class: t.key === L.current ? "active" : "", onclick: () => selectTab(t.key),
                                        title: ids.map((id) => `${nameOf(id)} - opened by ${CONTROLLER_NAMES[(info(id) || {}).opened_by] || (info(id) || {}).opened_by || "?"}`).join("\n") },
            icon(serial ? "plug" : "cmd"), label(first),
            ids.length > 1 ? h("span.n", { title: ids.length + " panes" }, "+" + (ids.length - 1)) : null,
            h("span.v", null, viewers ? `${viewers} viewing` : "idle"),
            h("span.x", { role: "button", "aria-label": serial ? "Close port" : "Close tab", title: ids.length > 1 ? "Close the tab's shells" : serial ? "Close port" : "Close shell",
                          onclick: (e) => { e.stopPropagation(); closeShells(ids); } }, icon("x")));
        }),
        h("button.btn.sm.term-new", { title: serial ? newLabel : "New tab (Ctrl+Shift+T)", "aria-label": newLabel,
                                      onclick: () => newTab() }, icon("plus"), h("span.lbl", null, newLabel)),
        !serial && panes ? h("span.term-tools", null,
          h("button.btn.sm.ghost.term-split", { "data-split": "row", title: "Split right (Ctrl+Alt+R)", "aria-label": "Split right",
                                                onclick: () => split("row") }, splitIcon("row")),
          h("button.btn.sm.ghost.term-split", { "data-split": "col", title: "Split down (Ctrl+Alt+D)", "aria-label": "Split down",
                                                onclick: () => split("col") }, splitIcon("col"))) : null,
        L.tabs.length ? h("button.btn.sm.ghost.term-fullbtn", { title: "Full screen (F11)", onclick: () => setFull(true) },
                          icon("expand"), "Full screen") : null,
        h("button.btn.sm.ghost.term-exitfull", { type: "button", title: "Leave full screen (F11)", "aria-label": "Leave full screen",
                                                 onclick: () => setFull(false) }, icon("shrink")));
    }

    // ------------------------------------------------------------ the panes of the current tab
    function render() {
      renderTabs();
      const tb = current();
      if (!tb) {
        for (const t of mgr.terms.values()) t.el.remove();
        clear(box, h("div.empty", { style: { paddingTop: "80px" } }, icon(serial ? "plug" : "terminal"),
          h("div", null, serial ? "No serial console open." : "No shells open."),
          h("div", { style: { marginTop: "14px" } }, h("button.btn.primary", { onclick: () => newTab() }, icon("plus"),
            serial ? "Open a serial port" : "Open a shell"))));
        return;
      }
      if (!ready) return;
      const shown = tb.zoom !== undefined ? { t: tb.zoom } : tb.root;
      const many = leaves(tb.root).length > 1;
      for (const [id, t] of mgr.terms) if (!leaves(shown).includes(id)) t.el.remove();
      clear(box, build(shown, many, tb));
      for (const id of leaves(shown)) attach(id);
      requestAnimationFrame(() => { fitAll(); focusActive(); });
    }

    function build(n, many, tb) {
      if (n.t !== undefined) return pane(n.t, many, tb);
      const a = build(n.a, many, tb), b = build(n.b, many, tb);
      const el = h("div.term-split.sp-" + n.dir, null, a, h("div.term-div", { role: "separator", "aria-orientation": n.dir === "row" ? "vertical" : "horizontal" }), b);
      const apply = () => { a.style.flex = `${n.r} 1 0`; b.style.flex = `${1 - n.r} 1 0`; };
      apply();
      el.children[1].addEventListener("pointerdown", (e) => {     // drag the divider
        e.preventDefault();
        const div = e.currentTarget;
        div.setPointerCapture(e.pointerId);
        const r0 = el.getBoundingClientRect();
        let raf = 0;
        const move = (ev) => {
          const p = n.dir === "row" ? (ev.clientX - r0.left) / r0.width : (ev.clientY - r0.top) / r0.height;
          n.r = Math.min(0.9, Math.max(0.1, p));
          apply();
          if (!raf) raf = requestAnimationFrame(() => { raf = 0; fitAll(); });
        };
        const up = () => { div.removeEventListener("pointermove", move); div.removeEventListener("pointerup", up); div.removeEventListener("pointercancel", up); fitAll(); };
        div.addEventListener("pointermove", move);
        div.addEventListener("pointerup", up);
        div.addEventListener("pointercancel", up);
      });
      return el;
    }

    function pane(id, many, tb) {
      const t = mgr.terms.get(id) || makeXterm(id);
      const zoomed = tb.zoom === id;
      const bar = many ? h("div.term-pane-bar", null,
        h("span.name", null, nameOf(id)),
        serial ? null : h("button", { title: "Split right (Ctrl+Alt+R)", "aria-label": "Split right", onclick: () => { focusPane(id); split("row"); } }, splitIcon("row")),
        serial ? null : h("button", { title: "Split down (Ctrl+Alt+D)", "aria-label": "Split down", onclick: () => { focusPane(id); split("col"); } }, splitIcon("col")),
        h("button", { title: zoomed ? "Restore (Ctrl+Shift+X)" : "Maximize (Ctrl+Shift+X)", "aria-label": zoomed ? "Restore pane" : "Maximize pane",
                      onclick: () => { focusPane(id); toggleZoom(); } }, icon(zoomed ? "shrink" : "expand")),
        h("button", { title: "Close (Ctrl+Shift+W)", "aria-label": "Close pane", onclick: () => closeShells([id]) }, icon("x"))) : null;
      const host = h("div.term-pane-body");
      const p = h("div.term-pane", { class: mgr.active === id && many ? "focus" : "", "data-term": String(id),
                                     onpointerdown: () => focusPane(id, true) }, bar, host);
      host.append(t.el);
      t.pane = p;
      return p;
    }

    async function attach(id) {
      const t = mgr.terms.get(id);
      if (!t) return;
      if (!t.opened) { t.xterm.open(t.el); t.opened = true; t.xterm.textarea?.addEventListener("focus", () => focusPane(id, true)); }
      if (t.attached) return;
      t.attached = true;
      try {
        const r = await conn.call("term.attach", { term: id, since: t.received });
        if (r.gap || !t.received) { if (t.received) t.xterm.reset(); t.received = r.start; }
      } catch (e) {
        t.attached = false;
        toast(errText(e), "err");
      }
    }

    function fitAll() {
      const tb = current();
      if (!tb) return;
      for (const id of leaves(tb.zoom !== undefined ? { t: tb.zoom } : tb.root)) {
        const t = mgr.terms.get(id);
        if (!t || !t.el.isConnected || !t.el.offsetWidth) continue;
        try { t.fit.fit(); } catch (e) { continue; }
        const size = [t.xterm.cols, t.xterm.rows];
        if (size[0] !== t.size[0] || size[1] !== t.size[1]) {
          t.size = size;
          conn.call("term.resize", { term: id, cols: size[0], rows: size[1] }).catch(() => {});
        }
      }
    }

    function focusActive() { mgr.terms.get(mgr.active)?.xterm.focus(); }

    function focusPane(id, fromPane = false) {
      if (mgr.active === id) return;
      mgr.active = id;
      for (const t of mgr.terms.values()) t.pane?.classList.toggle("focus", false);
      const tb = current();
      if (tb && leaves(tb.root).length > 1) mgr.terms.get(id)?.pane?.classList.add("focus");
      if (!fromPane) focusActive();
    }

    function selectTab(key) {
      L.current = key;
      const tb = current();
      mgr.active = tb ? (tb.zoom !== undefined ? tb.zoom : leaves(tb.root)[0]) : null;
      render();
    }

    function toggleZoom() {
      const tb = current();
      if (!tb || leaves(tb.root).length < 2) return;
      tb.zoom = tb.zoom === undefined ? mgr.active : undefined;
      render();
    }

    /** Alt+arrow: the pane whose centre lies that way, nearest first (null: none - the key is the shell's). */
    function paneToward(dx, dy) {
      const tb = current();
      if (!tb || tb.zoom !== undefined) return null;
      const from = mgr.terms.get(mgr.active)?.pane?.getBoundingClientRect();
      if (!from) return null;
      const cx = from.left + from.width / 2, cy = from.top + from.height / 2;
      let best = null, bestD = Infinity;
      for (const id of leaves(tb.root)) {
        if (id === mgr.active) continue;
        const r = mgr.terms.get(id)?.pane?.getBoundingClientRect();
        if (!r) continue;
        const x = r.left + r.width / 2 - cx, y = r.top + r.height / 2 - cy;
        if ((dx && Math.sign(x) !== dx) || (dy && Math.sign(y) !== dy)) continue;
        const d = dx ? Math.abs(x) + 2 * Math.abs(y) : Math.abs(y) + 2 * Math.abs(x);
        if (d < bestD) { bestD = d; best = id; }
      }
      return best;
    }

    /** TERM-05: the terminal over the whole window - and the whole screen where the browser
     *  allows it, with the keyboard held (as a native terminal holds it: the browser's own
     *  shortcuts reach the shell). Leaving the browser's full screen (Esc held) keeps the
     *  terminal over the window; only F11 or the button end the mode. */
    function setFull(on) {
      if (on === full) return;
      full = on;
      wrap.classList.toggle("full", on);
      document.body.classList.toggle("term-fullscreen", on);
      try {
        if (on && wrap.requestFullscreen && !document.fullscreenElement) {
          wrap.requestFullscreen({ navigationUI: "hide" }).then(() => navigator.keyboard?.lock?.())
            .catch(() => {});              // refused (no user gesture, iframe, iOS): the window is enough
        } else if (!on && document.fullscreenElement) {
          navigator.keyboard?.unlock?.();
          document.exitFullscreen().catch(() => {});
        }
      } catch (e) { /* older browsers: the CSS mode alone */ }
      requestAnimationFrame(() => { fitAll(); focusActive(); });
    }

    // F11 and the layout keys belong to this view while it is shown (before xterm: the shell never gets them)
    const onKey = (e) => {
      if (!wrap.isConnected) return;
      const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
      let act = null;
      if (k === "F11" && !e.ctrlKey && !e.altKey && !e.metaKey && L.tabs.length) act = () => setFull(!full);
      else if (e.ctrlKey && e.shiftKey && !e.altKey && k === "t") act = () => newTab();
      else if (e.ctrlKey && e.shiftKey && !e.altKey && k === "w" && mgr.active !== null) act = () => closeShells([mgr.active]);
      else if (e.ctrlKey && e.shiftKey && !e.altKey && k === "x" && current() && leaves(current().root).length > 1) act = () => toggleZoom();
      else if (!serial && e.ctrlKey && e.altKey && !e.shiftKey && k === "r") act = () => split("row");
      else if (!serial && e.ctrlKey && e.altKey && !e.shiftKey && k === "d") act = () => split("col");
      else if (e.altKey && !e.ctrlKey && !e.shiftKey && !e.metaKey && k.startsWith("Arrow")) {
        const to = paneToward(k === "ArrowLeft" ? -1 : k === "ArrowRight" ? 1 : 0, k === "ArrowUp" ? -1 : k === "ArrowDown" ? 1 : 0);
        if (to !== null) act = () => focusPane(to);
      }
      if (!act) return;
      e.preventDefault();
      e.stopPropagation();
      act();
    };
    window.addEventListener("keydown", onKey, true);

    async function openShell(dir) {
      if (!ready) return;
      try {
        const r = await conn.call("term.open", { cols: 100, rows: 30 });
        if (!mgr.terms.has(r.term)) {
          const nt = makeXterm(r.term);
          nt.received = 0;
          nt.attached = true;              // term.open attaches the opener: its output is already ours
        }
        list = mine(store.get("terminals"));
        if (!list.some((x) => x.term === r.term)) list = [...list, { term: r.term, kind, viewers: 1 }];
        place(r.term, dir);
        render();
      } catch (e) {
        toast(errText(e), "err");
      }
    }
    const newTab = () => (serial ? opts.onNew && opts.onNew() : openShell(null));
    const split = (dir) => { if (!serial && current()) openShell(dir); };

    async function closeShells(ids) {
      const others = ids.filter((id) => ((info(id) || {}).viewers || 0) > (mgr.terms.has(id) ? 1 : 0));
      if (others.length || ids.length > 1) {
        const one = ids.length === 1;
        const ok = await confirmBox({
          title: one ? `Close ${serial ? (info(ids[0]) || {}).port || "port" : nameOf(ids[0])}?` : `Close ${ids.length} shells?`,
          ok: "Close", danger: true,
          text: others.length ? (serial ? "Another controller is watching this port. Closing it ends the console for everyone."
                                        : "Another controller is using it. Closing ends its programs for everyone.")
                              : "Their programs end." });
        if (!ok) return;
      }
      for (const id of ids) conn.call("term.close", { term: id }).catch((e) => toast(errText(e), "err"));
    }

    const ro = new ResizeObserver(() => fitAll());
    ro.observe(box);
    // a new size or viewer count only relabels the strip; the panes are rebuilt only when shells come
    // or go (a rebuild under a divider being dragged, or a pane being typed in, would drop it)
    const shellsOf = (v) => v.map((t) => t.term).join(",");
    const offs = [store.on("terminals", (v) => {
      const before = shellsOf(list);
      list = mine(v);
      if (shellsOf(list) === before) return renderTabs();
      reconcile();
      render();
    })];
    renderKeys();
    reconcile();
    render();
    loadXterm().then(() => {
      wire();
      ready = true;
      if (opts.term !== undefined && list.some((t) => t.term === opts.term)) show(opts.term);
      else { reconcile(); render(); }
    }).catch((e) => toast(errText(e), "err"));
    function show(id) {
      const tb = tabOf(id);
      if (tb) { L.current = tb.key; tb.zoom = tb.zoom === id ? id : undefined; mgr.active = id; }
      render();
    }
    root.showTerm = (id) => { if (ready) { reconcile(); show(id); } else opts.term = id; };

    return () => {
      setFull(false);
      window.removeEventListener("keydown", onKey, true);
      offs.forEach((f) => f());
      ro.disconnect();
      mgr.onChange = null;
      for (const t of mgr.terms.values()) t.el.remove();
    };
  },
};

/** The split glyphs: a frame with its divider. */
function splitIcon(dir) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", "0 0 24 24");
  el.setAttribute("class", "i");
  el.setAttribute("aria-hidden", "true");
  el.innerHTML = dir === "row" ? '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>'
                               : '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 12h18"/>';
  return el;
}
