// The backup terminal's page (ADM-09): log in, then the machine's shells in tabs. One xterm shows
// the current shell; a switch attaches it to another (the server replays its recent output).
// Every URL is relative: the page also works through the device manager (B/d/<id>/8181/).

const $ = (id) => document.getElementById(id);
const enc = new TextEncoder();
const KEYS = { esc: "\x1b", tab: "\t", "^c": "\x03", "^d": "\x04", up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D" };
let term = null, fit = null, sock = null, current = null, shells = [], ctrl = false, timer = 0;
// the Arstro palette (docs/design.md), on the page background (the shell fills the page)
const THEME = {
  background: "#0c0c0b", foreground: "#ededea", cursor: "#ededea", cursorAccent: "#0c0c0b",
  selectionBackground: "rgba(237,237,234,.22)",
  black: "#34342f", red: "#ec7e76", green: "#7cc495", yellow: "#ddb764", blue: "#8fb3e0", magenta: "#c9a2d9",
  cyan: "#7fc4c4", white: "#d6d4cd", brightBlack: "#868379", brightRed: "#f29d96", brightGreen: "#9ed6b1",
  brightYellow: "#e8cc8e", brightBlue: "#b1cbea", brightMagenta: "#dcbfe7", brightCyan: "#a6d9d8", brightWhite: "#f7f7f4",
};

async function api(path, body) {
  const init = body === undefined ? { credentials: "same-origin" }
    : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(d.error || r.statusText); e.status = r.status; throw e; }
  return d;
}

function wsUrl(path) {
  const u = new URL(path, location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  return u.href;
}

function note(text) {
  $("note").textContent = text;
  $("note").hidden = !text;
}

// ------------------------------------------------------------------ login
function showLogin() {
  for (const id of ["term", "keys", "new", "logout"]) $(id).hidden = true;
  $("tabs").replaceChildren();
  $("login").hidden = false;
  $("pw").focus();
}

$("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-err").textContent = "";
  try {
    await api("api/login", { password: $("pw").value });
    $("pw").value = "";
    $("login").hidden = true;
    start();
  } catch (err) {
    $("login-err").textContent = err.message;
  }
});

$("logout").addEventListener("click", async () => {
  await api("api/logout", {}).catch(() => {});
  location.reload();
});

// ------------------------------------------------------------------ shells
function setup() {
  if (term) return;
  term = new window.Terminal({ cursorBlink: true, scrollback: 5000, fontSize: 14,
    fontFamily: "ui-monospace, 'DejaVu Sans Mono', Menlo, Consolas, monospace", theme: THEME });
  fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("term"));
  term.onData((d) => send(d));
  term.onResize(({ cols, rows }) => {
    if (sock && sock.readyState === 1) sock.send(JSON.stringify({ t: "resize", cols, rows }));
  });
  new ResizeObserver(() => { try { fit.fit(); } catch (e) { /* hidden */ } }).observe($("term"));
  for (const b of document.querySelectorAll("#keys button")) {
    b.addEventListener("click", () => {
      const k = b.dataset.k;
      if (k === "ctrl") { ctrl = !ctrl; b.classList.toggle("on", ctrl); }
      else send(KEYS[k]);
      term.focus();
    });
  }
}

function send(d) {
  if (ctrl && d.length === 1 && /[a-z@\[\\\]^_ ]/i.test(d)) {        // sticky Ctrl from the key row
    d = String.fromCharCode(d.toUpperCase().charCodeAt(0) & 0x1f);
    ctrl = false;
    $("ctrl").classList.remove("on");
  }
  if (sock && sock.readyState === 1) sock.send(enc.encode(d));
}

function renderTabs() {
  $("tabs").replaceChildren(...shells.map((s, i) => {
    const close = document.createElement("button");
    close.className = "x";
    close.textContent = "×";
    close.title = "Close this shell";
    close.setAttribute("aria-label", "Close shell " + (i + 1));
    close.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`api/terms/${s.id}/close`, {}).catch((err) => note(err.message));
      refresh();
    });
    const tab = document.createElement("div");
    tab.className = "tab" + (s.id === current ? " on" : "");
    tab.setAttribute("role", "button");
    tab.tabIndex = 0;
    tab.append(`${i + 1} · ${s.shell}`, close);
    tab.addEventListener("click", () => attach(s.id));
    return tab;
  }));
}

async function refresh() {
  try {
    shells = (await api("api/terms")).terms;
  } catch (e) {
    if (e.status === 401) return showLogin();
    return note("No answer from the backup terminal: " + e.message);
  }
  if (!shells.length) return newShell();
  if (!shells.some((s) => s.id === current)) attach(shells[0].id);
  else renderTabs();
}

async function newShell() {
  try {
    const r = await api("api/terms", { cols: term.cols, rows: term.rows });
    shells = (await api("api/terms")).terms;
    attach(r.id);
  } catch (e) {
    note(e.message);
  }
}

function attach(id) {
  if (sock) { sock.onclose = null; sock.close(); }
  current = id;
  try { sessionStorage.setItem("arstro.backup.term", String(id)); } catch (e) { /* private mode */ }
  renderTabs();
  note("");
  term.reset();
  const s = new WebSocket(wsUrl(`ws/term?id=${id}&cols=${term.cols}&rows=${term.rows}`));
  s.binaryType = "arraybuffer";
  s.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const m = JSON.parse(ev.data);
      if (m.t === "exit") { term.write(`\r\n\x1b[2m[the shell ended (code ${m.code})]\x1b[0m\r\n`); setTimeout(refresh, 800); }
      return;
    }
    term.write(new Uint8Array(ev.data));
  };
  s.onclose = () => {                                   // dropped: attach again (the shell lives on)
    if (sock !== s) return;
    term.write("\r\n\x1b[2m[connection lost - reconnecting…]\x1b[0m\r\n");
    setTimeout(() => { if (sock === s) refresh().then(() => current === id && attach(id)); }, 2000);
  };
  sock = s;
  term.focus();
}

async function start() {
  try {
    const me = await api("api/me");
    $("host").textContent = me.host ? "· " + me.host : "";
  } catch (e) {
    if (e.status === 401) return showLogin();
    return note("No answer from the backup terminal: " + e.message);
  }
  for (const id of ["term", "new", "logout"]) $(id).hidden = false;
  $("keys").hidden = !matchMedia("(pointer: coarse)").matches;
  setup();
  fit.fit();
  let last = null;
  try { last = Number(sessionStorage.getItem("arstro.backup.term")); } catch (e) { /* private mode */ }
  shells = (await api("api/terms").catch(() => ({ terms: [] }))).terms;
  if (last && shells.some((s) => s.id === last)) attach(last);
  else if (shells.length) attach(shells[0].id);
  else newShell();
  clearInterval(timer);
  timer = setInterval(refresh, 5000);                   // shells opened or closed elsewhere
}

$("new").addEventListener("click", () => newShell());
window.backupDebug = { get term() { return term; }, get current() { return current; } };
start();
