// Core of the web controller: DOM helpers, icons, formatting, UI toolkit (toast, sheet,
// confirm, form), the shared state store and the WebSocket connection to the server.
//
// The web UI speaks the same protocol as the app and the CLI (docs/protocol.md):
// JSON ops out, replies + `state` events in, TERM frames for shells.

export const VERSION = "2.0.0";

// ------------------------------------------------------------------ DOM
export function h(tag, attrs, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = name === "svg" ? document.createElementNS("http://www.w3.org/2000/svg", "svg")
                            : document.createElement(name || "div");
  if (classes.length) el.setAttribute("class", classes.join(" "));
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.setAttribute("class", [el.getAttribute("class"), v].filter(Boolean).join(" "));
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "html") throw new Error("no innerHTML");
      else if (k in el && typeof v !== "string" && k !== "list") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

// ---------------------------------------------------------------- icons
const P = {
  logo: '<path d="M6 20 12 5l6 15M8.5 14h7"/>',
  recorder: '<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="m15.5 10 6-3.5v11l-6-3.5"/>',
  gallery: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="m21 15-4.5-4.5L6 21"/>',
  monitor: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  wifi: '<path d="M2 8.8a15 15 0 0 1 20 0"/><path d="M5.2 12.4a10 10 0 0 1 13.6 0"/><path d="M8.6 15.8a5 5 0 0 1 6.8 0"/><path d="M12 19.5h.01"/>',
  "wifi-off": '<path d="M3 3l18 18"/><path d="M8.6 15.8a5 5 0 0 1 6.8 0M5.2 12.4a10 10 0 0 1 4-2.3M2 8.8A15 15 0 0 1 7 5.6M14.5 10.2a10 10 0 0 1 4.3 2.2M11 4.1A15 15 0 0 1 22 8.8"/><path d="M12 19.5h.01"/>',
  terminal: '<rect x="2.5" y="4" width="19" height="16" rx="3"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  remote: '<rect x="6" y="2.5" width="12" height="19" rx="6"/><path d="M12 6.5v4"/>',
  system: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  convert: '<path d="M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5M20 12a8 8 0 0 1-14 5.3L4 15M4 20v-5h5"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6.5 6.5 0 0 1 3.5 6"/>',
  bluetooth: '<path d="m7 7 10 10-5 4V3l5 4L7 17"/>',
  link: '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2.5"/><path d="M16 8V5.5A2.5 2.5 0 0 0 13.5 3h-8A2.5 2.5 0 0 0 3 5.5v8A2.5 2.5 0 0 0 5.5 16H8"/>',
  key: '<circle cx="8" cy="15" r="4.5"/><path d="m11.5 11.5 8-8M16 7l3 3M18.5 4.5l2 2"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>',
  "no-signal": '<rect x="2.5" y="4.5" width="19" height="13" rx="2.5"/><path d="M8 21h8M4 3l16 16"/>',
  disk: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M5 13 7.5 5h9L19 13M7 16.5h.01"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9.5 9.5h5v5h-5zM9 2.5V6M15 2.5V6M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>',
  thermo: '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/>',
  memory: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 11v2M11 11v2M15 11v2M7 17v3M17 17v3"/>',
  net: '<path d="M7 17V5M3.5 8.5 7 5l3.5 3.5M17 7v12M13.5 15.5 17 19l3.5-3.5"/>',
  logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H4"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/>',
  film: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4"/>',
  volume: '<path d="M4 9.5v5h3.5L12 19V5L7.5 9.5z"/><path d="M16 9a4 4 0 0 1 0 6"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M7.5 14h9"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  up: '<path d="m6 15 6-6 6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  power: '<path d="M12 3v8M6.5 6.5a8 8 0 1 0 11 0"/>',
  phone: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  cmd: '<path d="m5 8 4 4-4 4M11 16h8"/>',
  signal: '<path d="M4 20v-3M9 20v-7M14 20V9M19 20V4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  send: '<path d="M4 12 20 4l-6 16-3-7z"/>',
  hidden: '<path d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3 3.9M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7a9.6 9.6 0 0 0 4.4-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
};

export function icon(name, cls = "") {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", "0 0 24 24");
  el.setAttribute("class", ("i " + cls).trim());
  el.setAttribute("aria-hidden", "true");
  // static, trusted markup from the table above (never server data)
  el.innerHTML = P[name] || P.info;
  return el;
}

// ----------------------------------------------------------- formatting
export function fmtBytes(n, digits = 1) {
  if (n === null || n === undefined || isNaN(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = Number(n);
  while (Math.abs(v) >= 1000 && i < u.length - 1) { v /= 1000; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(v >= 100 ? 0 : digits)) + " " + u[i];
}

export function fmtRate(bytesPerSec) {
  if (!bytesPerSec) return "0 Mb/s";
  const mbit = bytesPerSec * 8 / 1e6;
  return (mbit >= 100 ? mbit.toFixed(0) : mbit.toFixed(1)) + " Mb/s";
}

export function fmtDur(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "–";
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600), mm = Math.floor(sec / 60) % 60, ss = sec % 60;
  const p = (x) => String(x).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
}

export function fmtUptime(sec) {
  const d = Math.floor(sec / 86400), hh = Math.floor(sec / 3600) % 24, mm = Math.floor(sec / 60) % 60;
  return d ? `${d}d ${hh}h` : hh ? `${hh}h ${mm}m` : `${mm}m`;
}

export function fmtAgo(unix) {
  const s = Math.max(0, Date.now() / 1000 - unix);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}

export function signalBars(pct) {
  const n = pct >= 75 ? 4 : pct >= 50 ? 3 : pct >= 25 ? 2 : pct > 0 ? 1 : 0;
  return h("span.signal-bars", { title: pct + "%" }, [1, 2, 3, 4].map((i) => h("i", { class: i <= n ? "on" : "" })));
}

export const CONTROLLER_NAMES = { app: "Android app", web: "Web", cli: "CLI", remote: "Remote CLI", local: "Local" };

// ------------------------------------------------------------- toolkit
export function toast(msg, kind = "") {
  const box = document.getElementById("toasts");
  const el = h("div.toast", { class: kind }, kind === "err" ? icon("alert") : kind === "ok" ? icon("check") : null,
               h("span", null, msg));
  box.append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 260); }, kind === "err" ? 5200 : 3200);
}

export function errText(e) {
  return (e && (e.message || e.error)) || String(e);
}

/** Run `fn` with a spinner on `btn`; errors become a toast. Returns fn's result (or undefined). */
export async function busy(btn, fn) {
  if (btn) { btn.classList.add("busy"); btn.prepend(h("span.spin")); }
  try {
    return await fn();
  } catch (e) {
    toast(errText(e), "err");
    return undefined;
  } finally {
    if (btn) { btn.classList.remove("busy"); btn.querySelector(":scope > .spin")?.remove(); }
  }
}

let openSheets = [];

export function sheet({ title, body, foot, wide = false, onClose } = {}) {
  const overlay = h("div.overlay");
  const bodyEl = h("div.sheet-body", null, body);
  const footEl = foot ? h("div.sheet-foot", null, foot) : null;
  const titleEl = h("h2.ellipsis", null, title || "");
  const panel = h("div.sheet", { class: wide ? "wide" : "", role: "dialog", "aria-modal": "true" },
    h("div.grabber"),
    h("div.sheet-head", null, titleEl,
      h("button.btn.ghost.icon.sm", { "aria-label": "Close", onclick: () => api.close() }, icon("x"))),
    bodyEl, footEl);
  let closed = false;
  const api = {
    el: panel, body: bodyEl, foot: footEl,
    setTitle: (t) => { titleEl.textContent = t; },
    close() {
      if (closed) return;
      closed = true;
      openSheets = openSheets.filter((s) => s !== api);
      overlay.classList.remove("show");
      panel.classList.remove("show");
      setTimeout(() => { overlay.remove(); panel.remove(); }, 300);
      onClose && onClose();
    },
    get closed() { return closed; },
  };
  overlay.addEventListener("click", () => api.close());
  document.body.append(overlay, panel);
  requestAnimationFrame(() => requestAnimationFrame(() => { overlay.classList.add("show"); panel.classList.add("show"); }));
  openSheets.push(api);
  return api;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && openSheets.length) openSheets[openSheets.length - 1].close();
});

export function closeAllSheets() {
  for (const s of [...openSheets]) s.close();
}

export function confirmBox({ title, text, ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    let result = false;
    const okBtn = h("button.btn", { class: danger ? "danger solid" : "primary", onclick: () => { result = true; s.close(); } }, ok);
    const s = sheet({
      title, body: h("p.dim", { style: { margin: "4px 0 6px" } }, text),
      foot: [h("button.btn.ghost", { onclick: () => s.close() }, "Cancel"), okBtn],
      onClose: () => resolve(result),
    });
    setTimeout(() => okBtn.focus(), 50);
  });
}

/** A small form in a sheet. fields: [{name, label, type, value, placeholder, hint}] → values or null. */
export function formBox({ title, fields, ok = "OK", text }) {
  return new Promise((resolve) => {
    let result = null;
    const inputs = {};
    const form = h("form.stack", {
      onsubmit: (e) => {
        e.preventDefault();
        result = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.type === "checkbox" ? el.checked : el.value]));
        s.close();
      },
    },
      text ? h("p.dim", { style: { margin: "0" } }, text) : null,
      fields.map((f) => {
        const el = h("input.input", { type: f.type || "text", name: f.name, value: f.value || "", placeholder: f.placeholder || "",
                                      autocomplete: f.autocomplete || "off", spellcheck: "false" });
        inputs[f.name] = el;
        return h("div.field", null, h("label", null, f.label), el, f.hint ? h("div.hint", null, f.hint) : null);
      }),
      h("button", { type: "submit", class: "hidden" }));
    const s = sheet({
      title, body: form,
      foot: [h("button.btn.ghost", { onclick: () => s.close() }, "Cancel"),
             h("button.btn.primary", { onclick: () => form.requestSubmit() }, ok)],
      onClose: () => resolve(result),
    });
    setTimeout(() => Object.values(inputs)[0]?.focus(), 80);
  });
}

export function seg(options, value, onChange, { full = false, disabled = {} } = {}) {
  const el = h("div.seg", { class: full ? "full" : "", role: "radiogroup" });
  const render = (v) => {
    clear(el, options.map(([val, label]) => h("button", {
      type: "button", class: val === v ? "active" : "", disabled: !!disabled[val], role: "radio",
      "aria-checked": String(val === v), onclick: () => { if (val !== v) { render(val); onChange(val); } },
    }, label)));
  };
  render(value);
  el.set = render;
  return el;
}

export function toggle(checked, onChange, { disabled = false, label } = {}) {
  const input = h("input", { type: "checkbox", checked: !!checked, disabled, "aria-label": label || "" });
  input.addEventListener("change", () => onChange(input.checked));
  const el = h("label.switch", null, input, h("span"));
  el.set = (v) => { input.checked = !!v; };
  el.input = input;
  return el;
}

export function copyText(text) {
  const done = () => toast("Copied", "ok");
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(done);
  const ta = h("textarea", { style: { position: "fixed", opacity: "0" } }, text);
  document.body.append(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed", "err"); }
  ta.remove();
  return Promise.resolve();
}

export function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[data-src="${src}"]`)) return resolve();
    const s = h("script", { src, "data-src": src });
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("failed to load " + src));
    document.head.append(s);
  });
}

export function loadStyle(href) {
  if (!document.querySelector(`link[href="${href}"]`)) document.head.append(h("link", { rel: "stylesheet", href }));
}

// --------------------------------------------------------------- store
/** Latest data of every state topic (ARC-03) + subscribers. */
export const store = {
  data: {},
  subs: new Map(),
  get(topic) { return this.data[topic]; },
  set(topic, value) {
    this.data[topic] = value;
    for (const fn of [...(this.subs.get(topic) || [])]) {
      try { fn(value); } catch (e) { console.error(e); }
    }
  },
  on(topic, fn, { now = true } = {}) {
    if (!this.subs.has(topic)) this.subs.set(topic, new Set());
    this.subs.get(topic).add(fn);
    if (now && this.data[topic] !== undefined) fn(this.data[topic]);
    return () => this.subs.get(topic)?.delete(fn);
  },
};

// ---------------------------------------------------------- connection
const T_JSON = 1, T_TERM = 2, T_TERM_OUT = 3;
const enc = new TextEncoder();
const dec = new TextDecoder();

export class Connection {
  constructor() {
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.chunks = [];
    this.buffered = 0;
    this.status = "idle";          // idle | connecting | open | wait | unauthorized
    this.hello = null;
    this.backoff = 500;
    this.handlers = { status: new Set(), ready: new Set(), event: new Set(), term: new Set() };
    this.wanted = false;
  }

  on(what, fn) { this.handlers[what].add(fn); return () => this.handlers[what].delete(fn); }
  emit(what, ...args) {
    for (const fn of [...this.handlers[what]]) {
      try { fn(...args); } catch (e) { console.error(e); }
    }
  }
  setStatus(s) { if (this.status !== s) { this.status = s; this.emit("status", s); } }

  start() { this.wanted = true; this.open(); }

  stop() {
    this.wanted = false;
    this.hello = null;
    clearTimeout(this.retryTimer);
    if (this.ws) this.ws.close();
  }

  open() {
    clearTimeout(this.retryTimer);
    this.setStatus("connecting");
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    this.chunks = [];
    this.buffered = 0;
    ws.onopen = async () => {
      try {
        const hello = await this.call("hello", { app: "arstro-web", version: VERSION,
                                                 device: navigator.userAgent.slice(0, 120) }, 10000);
        this.hello = hello;
        for (const [topic, data] of Object.entries(hello.state || {})) store.set(topic, data);
        this.backoff = 500;
        this.setStatus("open");
        this.emit("ready", hello);
      } catch (e) {
        ws.close();
      }
    };
    ws.onmessage = (e) => this.onData(e.data);
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      for (const [, p] of this.pending) p.reject(new Error("connection lost"));
      this.pending.clear();
      if (!this.wanted) return this.setStatus("idle");
      this.setStatus("wait");
      this.checkAuth();
    };
  }

  async checkAuth() {
    try {
      const r = await fetch("/api/ping", { cache: "no-store" });
      const d = await r.json();
      if (!d.authorized) { this.wanted = false; this.setStatus("unauthorized"); return; }
    } catch (e) { /* server unreachable: keep retrying */ }
    this.retryTimer = setTimeout(() => this.open(), this.backoff);
    this.backoff = Math.min(this.backoff * 2, 5000);
  }

  onData(data) {
    if (typeof data === "string") {          // (the server sends binary; accept text anyway)
      this.onJson(JSON.parse(data));
      return;
    }
    this.chunks.push(new Uint8Array(data));
    this.buffered += data.byteLength;
    this.drain();
  }

  drain() {
    if (!this.buffered) return;
    let buf = this.chunks.length === 1 ? this.chunks[0] : concat(this.chunks, this.buffered);
    let pos = 0;
    while (buf.length - pos >= 5) {
      const len = ((buf[pos + 1] << 24) >>> 0) + (buf[pos + 2] << 16) + (buf[pos + 3] << 8) + buf[pos + 4];
      if (buf.length - pos - 5 < len) break;
      const type = buf[pos];
      const payload = buf.subarray(pos + 5, pos + 5 + len);
      pos += 5 + len;
      if (type === T_JSON) {
        try { this.onJson(JSON.parse(dec.decode(payload))); } catch (e) { console.error(e); }
      } else if (type === T_TERM_OUT && payload.length >= 9) {
        const term = payload[0];
        const dv = new DataView(payload.buffer, payload.byteOffset + 1, 8);
        const offset = Number(dv.getBigUint64(0));
        this.emit("term", term, offset, payload.subarray(9));
      }
    }
    const rest = buf.subarray(pos);
    this.chunks = rest.length ? [rest.slice()] : [];
    this.buffered = rest.length;
  }

  onJson(msg) {
    if (msg.id !== undefined && this.pending.has(msg.id) && msg.ev === undefined) {
      const p = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.ok) p.resolve(msg.data === undefined ? {} : msg.data);
      else p.reject(new Error(msg.error || "failed"));
      return;
    }
    if (msg.ev === "state") store.set(msg.topic, msg.data);
    else if (msg.ev) this.emit("event", msg.ev, msg);
  }

  call(op, args = {}, timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return reject(new Error("not connected"));
      const id = this.nextId++;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(op + " timed out"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ ...args, op, id }));
    });
  }

  notify(op, args = {}) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ ...args, op }));
  }

  termWrite(term, data) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const bytes = typeof data === "string" ? enc.encode(data) : data;
    const frame = new Uint8Array(6 + bytes.length);
    const len = bytes.length + 1;
    frame[0] = T_TERM;
    frame[1] = (len >>> 24) & 255; frame[2] = (len >>> 16) & 255; frame[3] = (len >>> 8) & 255; frame[4] = len & 255;
    frame[5] = term & 255;
    frame.set(bytes, 6);
    this.ws.send(frame);
  }

  get open_() { return this.status === "open"; }
}

function concat(chunks, total) {
  const out = new Uint8Array(total);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

export const conn = new Connection();

// ------------------------------------------------------------ helpers
export function when(topic, fn) { return store.on(topic, fn); }

/** Call an op; show errors as a toast; return the data or undefined. */
export async function run(op, args, { ok, btn } = {}) {
  return busy(btn, async () => {
    const r = await conn.call(op, args);
    if (ok) toast(ok, "ok");
    return r;
  });
}

export function isNarrow() { return window.matchMedia("(max-width: 899px)").matches; }
