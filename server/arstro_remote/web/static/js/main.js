// Web controller entry: login (CON-03), navigation shell, router, connection status.

import { h, clear, icon, conn, store, closeAllSheets, VERSION } from "./core.js";
import { login } from "./login.js";
import recorder from "./views/recorder.js";
import gallery from "./views/gallery.js";
import monitor from "./views/monitor.js";
import wifi from "./views/wifi.js";
import terminal from "./views/terminal.js";
import remote from "./views/remote.js";
import system from "./views/system.js";

const VIEWS = [recorder, gallery, monitor, wifi, terminal, remote, system];
const root = document.getElementById("root");
let current = null;          // {view, cleanup, el}
let shell = null;

// ------------------------------------------------------------------ login
async function ping() {
  const r = await fetch("/api/ping", { cache: "no-store" });
  return r.json();
}


function showLogin(info, message) {
  conn.stop();
  closeAllSheets();
  unmountView();
  if (shell) shell.offs.forEach((f) => f());
  shell = null;
  const input = h("input.input", { type: "password", placeholder: "Password", autocomplete: "current-password",
                                   spellcheck: "false", "aria-label": "Password", name: "password" });
  const user = h("input", { type: "text", name: "username", autocomplete: "username", value: "arstro",
                            style: { display: "none" }, "aria-hidden": "true" });   // helps password managers
  const err = h("div.err", null, message || "");
  const btn = h("button.btn.primary", { type: "submit" }, "Sign in");
  const card = h("div.login-card", null,
    h("div.logo", null, icon("logo")),
    h("h1", null, "Arstro Remote"),
    h("p", null, info && info.hostname ? `Sign in to ${info.hostname}` : "Sign in to your Pi"),
    h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        const tok = input.value.trim();
        if (!tok) return;
        btn.disabled = true;
        err.textContent = "";
        let ok = false;
        try { ok = await login(tok); } catch (x) { err.textContent = "The Pi is not reachable."; }
        btn.disabled = false;
        if (ok) return boot();
        if (!err.textContent) err.textContent = "That password is not right.";
        card.classList.remove("shake");
        void card.offsetWidth;
        card.classList.add("shake");
        input.select();
      },
    }, user, input, btn, err),
    h("p.hint", { style: { marginTop: "18px" } }, "Forgot it? The app shows it under Settings → Web access, or run ",
      h("span.mono", null, "arstro-remote web --show"), " on the Pi."));
  clear(root, h("div.login", null, card));
  setTimeout(() => input.focus(), 60);
}

// ------------------------------------------------------------------ shell
function buildShell() {
  const navItems = {};
  const tabItems = {};
  const hostEls = [];
  const dotEls = [];
  const hostLine = () => {
    const dot = h("span.conn-dot");
    const name = h("span.ellipsis");
    dotEls.push(dot);
    hostEls.push(name);
    return h("div.brand-host", null, dot, name);
  };
  const side = h("nav.side", { "aria-label": "Main" },
    h("div.brand", null, h("div.logo", null, icon("logo")),
      h("div", { style: { minWidth: 0 } }, h("div.brand-name", null, "Arstro Remote"), hostLine())),
    VIEWS.map((v) => (navItems[v.id] = h("button.nav-item", { onclick: () => go(v.id) }, icon(v.icon), h("span", null, v.title)))),
    h("div.side-foot", null, "v" + VERSION));
  const title = h("div.topbar-title");
  const top = h("header.topbar", null, h("div.logo", null, icon("logo")), title, hostLine());
  const tabs = h("nav.tabbar", { "aria-label": "Main" },
    VIEWS.map((v) => (tabItems[v.id] = h("button.tab", { onclick: () => go(v.id), "aria-label": v.title }, icon(v.icon), h("span", null, v.title)))));
  const content = h("div.content");
  const main = h("main.main", null, top, content);
  clear(root, h("div.app", null, side, main, tabs));

  const setBadge = (id, on) => {
    for (const el of [navItems[id], tabItems[id]]) {
      const b = el.querySelector(".badge-dot");
      if (on && !b) el.append(h("span.badge-dot"));
      if (!on && b) b.remove();
    }
  };
  const offs = [
    store.on("recorder", (r) => setBadge("recorder", !!(r && r.recording && r.recording.active))),
    store.on("jobs", (j) => setBadge("gallery", (j || []).some((x) => x.state === "running"))),
  ];
  clear(content, h("div.empty", { style: { paddingTop: "120px" } }, h("span.spin", { style: { margin: "0 auto 12px" } }),
                   h("div", null, "Connecting…")));

  return {
    content, title, offs,
    select(id) {
      for (const v of VIEWS) {
        navItems[v.id].classList.toggle("active", v.id === id);
        tabItems[v.id].classList.toggle("active", v.id === id);
        navItems[v.id].setAttribute("aria-current", v.id === id ? "page" : "false");
      }
    },
    setHost(name) { hostEls.forEach((el) => { el.textContent = name; }); },
    setStatus(s) {
      dotEls.forEach((d) => { d.className = "conn-dot " + ({ open: "on", wait: "wait", connecting: "wait" }[s] || "off"); });
    },
  };
}

function unmountView() {
  if (current) {
    try { current.cleanup && current.cleanup(); } catch (e) { console.error(e); }
    current = null;
  }
}

function route() {
  const id = (location.hash.replace(/^#\/?/, "") || "recorder").split("?")[0];
  return VIEWS.find((v) => v.id === id) || recorder;
}

function go(id) {
  if (location.hash !== "#/" + id) location.hash = "#/" + id;
  else render();
}

function render() {
  if (!shell || !conn.hello) return;          // views mount once the server said hello
  const view = route();
  if (current && current.view === view) return;
  closeAllSheets();
  unmountView();
  const el = h("section.view", { "data-view": view.id });
  clear(shell.content, el);
  shell.select(view.id);
  shell.title.textContent = view.title;
  document.title = view.title + " · Arstro Remote";
  window.scrollTo({ top: 0 });
  let cleanup = null;
  try { cleanup = view.mount(el, { logout }); } catch (e) { console.error(e); }
  current = { view, cleanup, el };
}

async function logout() {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  showLogin(null, "Signed out.");
}

// ------------------------------------------------------------------- boot
let wired = false;

async function boot() {
  const params = new URLSearchParams(location.search);
  const urlToken = params.get("token");
  if (urlToken) {                       // ?token=… (QR codes, the app's "open in browser")
    history.replaceState(null, "", location.pathname + location.hash);
    await login(urlToken).catch(() => false);
  }
  let info;
  try {
    info = await ping();
  } catch (e) {
    clear(root, h("div.login", null, h("div.login-card", null, h("div.logo", null, icon("logo")),
      h("h1", null, "Can't reach the Pi"), h("p", null, "Check the network and reload."),
      h("button.btn.primary", { onclick: () => location.reload() }, "Reload"))));
    return;
  }
  if (!info.authorized) return showLogin(info, urlToken ? "That password is not right." : "");

  shell = buildShell();
  shell.setHost(info.hostname || "");
  if (!wired) {
    wired = true;
    const banner = document.getElementById("banner");
    const bannerText = document.getElementById("banner-text");
    let bannerTimer = null;
    conn.on("status", (s) => {
      shell && shell.setStatus(s);
      clearTimeout(bannerTimer);
      if (s === "wait" || s === "connecting") {
        bannerText.textContent = "Reconnecting…";
        bannerTimer = setTimeout(() => banner.classList.add("show"), s === "wait" ? 400 : 1500);
      } else {
        banner.classList.remove("show");
      }
      if (s === "unauthorized") showLogin(null, "The password changed. Sign in again.");
    });
    conn.on("ready", (hello) => {
      if (!shell) return;
      shell.setHost(hello.hostname);
      if (!current) render();
    });
    conn.on("event", (ev, msg) => {
      if (ev === "closing" && msg.reason) console.info("server closing session:", msg.reason);
    });
    window.addEventListener("hashchange", render);
  }
  conn.start();
  shell.setStatus(conn.status);
  render();
}

boot();
