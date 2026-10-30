// The phone's web UI shell (PHN-17): header (name, model, battery, online), navigation by feature
// (features that are off or not ready are shown greyed with the reason - PHN-02), the router
// (#/<page>) and the /ws event connection. Pages are loaded when first opened (fewer requests
// over the device manager's channel).

import { h, clear, icon, store, events, getJson, toast, closeAllSheets, feature, featureReady, featureReason, onEvent,
         pref, VERSION, errText, batteryPct } from "./core.js";

const VIEWS = [
  { id: "overview", title: "Overview", icon: "overview", load: () => import("./views/overview.js") },
  { id: "screen", title: "Screen", icon: "screen", feature: "screen", load: () => import("./views/screen.js") },
  { id: "camera", title: "Camera", icon: "camera", feature: "camera", load: () => import("./views/camera.js") },
  { id: "call", title: "Call", icon: "call", feature: "call", load: () => import("./views/call.js") },
  { id: "listen", title: "Listen", icon: "mic", feature: "mic", load: () => import("./views/listen.js") },
  { id: "messages", title: "Messages", icon: "message", feature: "messages", load: () => import("./views/messages.js") },
  { id: "contacts", title: "Contacts", icon: "users", feature: "contacts", load: () => import("./views/contacts.js") },
  { id: "find", title: "Find my phone", icon: "pin", feature: "find", load: () => import("./views/find.js") },
  { id: "files", title: "Files", icon: "folder", feature: "files", load: () => import("./views/files.js") },
  { id: "notifications", title: "Notifications", icon: "bell", feature: "notifications", load: () => import("./views/notifications.js") },
  { id: "clipboard", title: "Clipboard", icon: "clipboard", feature: "clipboard", load: () => import("./views/clipboard.js") },
  { id: "apps", title: "Apps", icon: "apps", feature: "apps", load: () => import("./views/apps.js") },
  { id: "tools", title: "Tools", icon: "sliders", feature: "tools", load: () => import("./views/tools.js") },
  { id: "shell", title: "Shell", icon: "terminal", feature: "shell", load: () => import("./views/shell.js") },
  { id: "features", title: "Features", icon: "toggles", load: () => import("./views/features.js") },
];

const root = document.getElementById("root");
let shell = null;
let current = null;           // {id, cleanup, ready}
let token = 0;                // the latest render (a slow import must not mount over a newer page)
const unseen = {};            // nav badges: events that came while their page was not open
function setBadge(id, n) { unseen[id] = n; shell && shell.setBadge(id, n); }

// ------------------------------------------------------------------ theme
function applyTheme() {
  const t = pref.get("theme", "auto");
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  const dark = t === "dark" || (t === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#0a0b0d" : "#f6f7f9");
  return t;
}

function cycleTheme() {
  const order = ["auto", "dark", "light"];
  const t = order[(order.indexOf(pref.get("theme", "auto")) + 1) % order.length];
  pref.set("theme", t);
  applyTheme();
  shell?.renderTheme();
}

// ------------------------------------------------------------------ shell
function buildShell() {
  const nameEls = [], modelEls = [], battEls = [], connEls = [];
  const battery = () => { const el = h("span.batt"); battEls.push(el); return el; };
  const conn = () => { const el = h("span.conn", null, h("span.conn-dot"), h("span.conn-text")); connEls.push(el); return el; };

  const nav = h("div.nav-list");
  const themeBtn = h("button.btn.ghost.sm.theme-btn", { onclick: cycleTheme, "aria-label": "Theme" });
  const side = h("nav.side", { "aria-label": "Phone" },
    h("div.brand", null,
      h("div.logo", null, icon("phone")),
      h("div.brand-txt", null,
        (() => { const n = h("div.brand-name.ellipsis"); nameEls.push(n); return n; })(),
        (() => { const m = h("div.brand-model.ellipsis"); modelEls.push(m); return m; })()),
      h("button.btn.ghost.icon.sm.drawer-x", { "aria-label": "Close menu", onclick: () => setDrawer(false) }, icon("x"))),
    h("div.brand-status", null, conn(), battery()),
    nav,
    h("div.side-foot", null, themeBtn, h("span.muted", null, "v" + VERSION)));
  const title = h("div.topbar-title.ellipsis");
  const top = h("header.topbar", null,
    h("button.btn.ghost.icon.menu-btn", { "aria-label": "Menu", onclick: () => setDrawer(true) }, icon("menu")),
    h("div.topbar-txt", null, title, (() => { const n = h("div.topbar-sub.ellipsis"); nameEls.push(n); return n; })()),
    h("div.topbar-right", null, battery(), conn()));
  const content = h("div.content");
  const scrim = h("div.scrim", { onclick: () => setDrawer(false) });
  const app = h("div.app", null, side, h("main.main", null, top, content), scrim);
  clear(root, app);

  function setDrawer(open) {
    app.classList.toggle("drawer-open", open);
    if (open) nav.querySelector(".active")?.focus({ preventScroll: true });
  }

  const items = {};
  function renderNav() {
    const on = [], off = [];
    for (const v of VIEWS) {
      const f = v.feature ? feature(v.feature) : null;
      if (v.feature && !f) continue;                      // this phone has no such feature
      const ready = !v.feature || (f.on && f.ready);
      const why = !v.feature ? "" : !f.on ? "Off on the phone" : !f.ready ? "Needs " + missingText(f) : "";
      const el = h("a.nav-item", {
        href: "#/" + v.id, class: ready ? "" : "grey", "data-id": v.id, title: why ? featureReason(v.feature) : v.title,
        onclick: () => setDrawer(false),
      }, icon(v.icon), h("span.nav-txt", null, h("span.ellipsis", null, v.title), why ? h("small.ellipsis", null, why) : null),
         h("span.nav-badge"));
      items[v.id] = el;
      (v.feature && !f.on ? off : on).push(el);
    }
    clear(nav, on, off.length ? h("div.nav-group", null, "Off on the phone") : null, off);
    select(current && current.id);
    for (const [id, n] of Object.entries(badges)) setBadge(id, n);
  }

  const badges = {};
  function setBadge(id, n) {
    badges[id] = n;
    const b = items[id]?.querySelector(".nav-badge");
    if (b) { b.textContent = n ? (n > 99 ? "99+" : String(n)) : ""; b.classList.toggle("on", !!n); }
  }

  function select(id) {
    for (const [k, el] of Object.entries(items)) {
      el.classList.toggle("active", k === id);
      if (k === id) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current");
    }
  }

  function renderStatus(st) {
    const b = st && st.battery;
    for (const el of battEls) {
      if (!b || b.level === undefined || b.level === null) { clear(el); continue; }
      const pct = batteryPct(b.level);
      el.className = "batt" + (b.charging ? " charging" : "") + (pct <= 15 && !b.charging ? " low" : "");
      el.title = `Battery ${pct}%` + (b.charging ? ", charging" : "");
      clear(el, h("span.batt-icon", null, h("i", { style: { width: Math.max(4, pct) + "%" } })),
            b.charging ? icon("bolt", "bolt") : null, h("span.num", null, pct + "%"));
    }
  }

  function renderConn(s) {
    const text = { open: "Online", connecting: "Connecting…", wait: "Reconnecting…", idle: "Offline" }[s] || s;
    for (const el of connEls) {
      el.className = "conn " + ({ open: "on", wait: "wait", connecting: "wait" }[s] || "off");
      el.title = text;
      el.lastChild.textContent = text;
    }
  }

  function renderInfo(info) {
    for (const el of nameEls) el.textContent = info.name || info.model || "Phone";
    const model = [info.manufacturer && !String(info.model || "").toLowerCase().startsWith(String(info.manufacturer).toLowerCase())
                   ? info.manufacturer : null, info.model, info.android ? "Android " + info.android : null].filter(Boolean).join(" · ");
    for (const el of modelEls) el.textContent = model;
  }

  function renderTheme() {
    const t = pref.get("theme", "auto");
    clear(themeBtn, icon(t === "light" ? "sun" : t === "dark" ? "moon" : "theme"), { auto: "Auto", dark: "Dark", light: "Light" }[t]);
    themeBtn.title = "Theme: " + t + " (click to change)";
  }

  return { content, title, renderNav, renderStatus, renderConn, renderInfo, renderTheme, select, setBadge, setDrawer };
}

function missingText(f) {
  const miss = (f.missing || []).map((m) => (typeof m === "string" ? m : m.name)).filter(Boolean);
  return miss.length ? miss.join(", ") : "a permission";
}

// ------------------------------------------------------------------ router
function route() {
  const id = (location.hash.replace(/^#\/?/, "").split(/[/?]/)[0] || "overview");
  return VIEWS.find((v) => v.id === id) || VIEWS[0];
}

function unmount() {
  if (current && current.cleanup) {
    try { current.cleanup(); } catch (e) { console.error(e); }
  }
  current = null;
}

async function render(force = false) {
  if (!shell) return;
  const v = route();
  const ready = !v.feature || featureReady(v.feature);
  if (!force && current && current.id === v.id && current.ready === ready) return;
  const my = ++token;
  closeAllSheets();
  unmount();
  current = { id: v.id, cleanup: null, ready };
  shell.select(v.id);
  shell.title.textContent = v.title;
  document.title = v.title + " · " + ((store.get("info") || {}).name || "Phone");
  const el = h("section.view", { "data-view": v.id });
  clear(shell.content, el);
  window.scrollTo({ top: 0 });
  if (!ready) {
    el.append(blocked(v));
    return;
  }
  el.append(h("div.empty.loading", null, h("span.spin")));
  let mod;
  try {
    mod = await v.load();
  } catch (e) {
    if (my !== token) return;
    clear(el, h("div.empty", null, icon("alert"), h("b", null, "This page did not load"), h("div", null, errText(e)),
      h("button.btn.sm", { onclick: () => render(true), style: { marginTop: "12px" } }, icon("refresh"), "Try again")));
    return;
  }
  if (my !== token) return;
  clear(el);
  try {
    current.cleanup = mod.default.mount(el, { go, setBadge }) || null;
  } catch (e) {
    console.error(e);
    toast(errText(e), "err");
  }
}

function blocked(v) {
  const f = feature(v.feature);
  return h("div.blocked", null,
    h("div.blocked-ico", null, icon(f && !f.on ? "ban" : "lock")),
    h("h2", null, v.title),
    h("p", null, featureReason(v.feature)),
    f && f.note ? h("p.dim", null, f.note) : null,
    h("p.hint", null, "Switches and permissions are changed only on the phone, in the Arstro Remote Client app."),
    h("a.btn.sm", { href: "#/features" }, icon("toggles"), "All features"));
}

export function go(id) {
  if (location.hash !== "#/" + id) location.hash = "#/" + id;
  else render(true);
}

// ------------------------------------------------------------------- boot
async function loadInfo() {
  const info = await getJson("api/info");
  store.set("info", info);
  if (Array.isArray(info.features)) store.set("features", info.features);
  return info;
}

function bootError(e) {
  clear(root, h("div.boot", null, h("div.boot-card", null,
    h("div.logo", null, icon("phone")),
    h("h1", null, "Can't reach the phone"),
    h("p", null, errText(e)),
    h("p.hint", null, "The phone needs to be on and online, with Arstro Remote Client sharing it. Trying again…"),
    h("button.btn.primary", { onclick: () => boot() }, icon("refresh"), "Try again"))));
}

let wired = false;
let retryTimer = 0;

async function boot() {
  clearTimeout(retryTimer);
  applyTheme();
  try {
    await loadInfo();
  } catch (e) {
    bootError(e);
    retryTimer = setTimeout(boot, 5000);
    return;
  }
  shell = buildShell();
  shell.renderInfo(store.get("info"));
  shell.renderTheme();
  shell.renderNav();
  shell.renderConn(events.status);
  if (!wired) {
    wired = true;
    const banner = document.getElementById("banner");
    let bannerTimer = 0, wasOpen = false;
    events.on("status", (s) => {
      shell && shell.renderConn(s);
      clearTimeout(bannerTimer);
      if (s === "open") {
        banner.classList.remove("show");
        if (wasOpen) loadInfo().then(() => shell && shell.renderNav()).catch(() => {});   // the phone may have restarted
        wasOpen = true;
      } else if (s === "wait") bannerTimer = setTimeout(() => banner.classList.add("show"), 1500);
    });
    store.on("status", (st) => shell && shell.renderStatus(st));
    store.on("features", () => {
      if (!shell) return;
      shell.renderNav();
      const v = route();
      if (current && v.feature && featureReady(v.feature) !== current.ready) render(true);
    }, { now: false });
    store.on("info", (info) => shell && shell.renderInfo(info), { now: false });
    window.addEventListener("hashchange", () => render());
    onEvent("sms", (e) => {
      if (route().id === "messages") return;
      setBadge("messages", (unseen.messages || 0) + 1);
      toast(`New message from ${e.name || e.address || "unknown"}`);
    });
    onEvent("notification", (e) => {
      if (route().id === "notifications" || e.removed) return;
      setBadge("notifications", (unseen.notifications || 0) + 1);
    });
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") events.kick(); });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", applyTheme);
  }
  events.start();
  render(true);
}

window.phoneDebug = window.phoneDebug || {};
window.phoneDebug.go = go;
boot();
