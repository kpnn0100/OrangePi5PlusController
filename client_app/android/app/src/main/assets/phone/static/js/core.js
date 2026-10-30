// Core of the phone's web UI (PHN-17, docs/phone/PHONE.md): DOM helpers, icons, formatting, the
// UI toolkit (toast, sheet, confirm, form), ops, the /ws event connection and the topic store.
//
// The page lives at B/d/<phone id>/8080/ behind the device manager's service worker, so every URL
// here is RELATIVE to the page's directory (`api/info`, `ws/screen`) - never "/api/...".

export const VERSION = "1.0.0";

// ------------------------------------------------------------------ DOM
export function h(tag, attrs, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
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
  phone: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  overview: '<rect x="3.5" y="3.5" width="7" height="9" rx="2"/><rect x="13.5" y="3.5" width="7" height="5" rx="2"/><rect x="13.5" y="11.5" width="7" height="9" rx="2"/><rect x="3.5" y="15.5" width="7" height="5" rx="2"/>',
  screen: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="m10.5 9 3.5 2.5-3.5 2.5z"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  call: '<path d="M5.5 3.5h3.2l1.8 4.6-2.3 1.4a11 11 0 0 0 6.3 6.3l1.4-2.3 4.6 1.8v3.2a2 2 0 0 1-2 2A16.5 16.5 0 0 1 3.5 5.5a2 2 0 0 1 2-2z"/>',
  "call-end": '<path d="M2.6 14.2c5.3-5 13.5-5 18.8 0l-2.1 2.9-4.3-1.6v-2.8a10.6 10.6 0 0 0-6 0v2.8l-4.3 1.6z"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>',
  "mic-off": '<path d="M3 3l18 18M9 9v2a3 3 0 0 0 5.1 2.1M15 10V6a3 3 0 0 0-5.8-1.1M5.5 11a6.5 6.5 0 0 0 10.9 4.8M18.5 11a6.4 6.4 0 0 1-.6 2.7M12 17.5V21M8.5 21h7"/>',
  message: '<path d="M4 4.5h16a1 1 0 0 1 1 1v10.5a1 1 0 0 1-1 1h-9l-5 4v-4H4a1 1 0 0 1-1-1V5.5a1 1 0 0 1 1-1z"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6.5 6.5 0 0 1 3.5 6"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>',
  pin: '<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  bell: '<path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  ring: '<path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0M2.5 8.5A9 9 0 0 1 5.5 3.5M21.5 8.5a9 9 0 0 0-3-5"/>',
  clipboard: '<rect x="5" y="4.5" width="14" height="17" rx="2.5"/><path d="M9 4.5v-1h6v1M9 11h6M9 15h4"/>',
  apps: '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
  sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  terminal: '<rect x="2.5" y="4" width="19" height="16" rx="3"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  toggles: '<rect x="2.5" y="6" width="19" height="12" rx="6"/><circle cx="15.5" cy="12" r="3.2"/>',
  battery: '<rect x="2.5" y="7" width="17" height="10" rx="2.5"/><path d="M21.5 10.5v3"/>',
  bolt: '<path d="M13 2.5 5 13.5h6l-1 8 8-11h-6z"/>',
  back: '<path d="M16 5.5 8 12l8 6.5z"/>',
  home: '<circle cx="12" cy="12" r="7"/>',
  recents: '<rect x="5.5" y="5.5" width="13" height="13" rx="2"/>',
  shade: '<path d="M4 4h16M4 8.5h16M8 13l4 4 4-4M12 17V11"/>',
  quick: '<rect x="3.5" y="3.5" width="7.5" height="7.5" rx="3.75"/><rect x="13" y="3.5" width="7.5" height="7.5" rx="3.75"/><rect x="3.5" y="13" width="7.5" height="7.5" rx="3.75"/><rect x="13" y="13" width="7.5" height="7.5" rx="3.75"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  unlock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7a4 4 0 0 1 7.7-1.5"/>',
  power: '<path d="M12 3v8M6.5 6.5a8 8 0 1 0 11 0"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  theme: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17M12 7.5h5.5M12 11.5h8M12 15.5h6.5"/>',
  torch: '<path d="M7 3h10v4l-2 3v11H9V10L7 7z"/><path d="M12 13.5v2.5M7 7h10"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  left: '<path d="m15 6-6 6 6 6"/>',
  right: '<path d="m9 6 6 6-6 6"/>',
  up: '<path d="m6 15 6-6 6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  upload: '<path d="M12 20V9M7 14l5-5 5 5M5 4h14"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
  flip: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><path d="M9 12.5a3 3 0 0 1 5.2-1.6M15 13.5a3 3 0 0 1-5.2 1.6M14.5 9v2h-2M9.5 17v-2h2"/>',
  record: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  playpause: '<path d="M4 6v12l8-6zM15 6v12M20 6v12"/>',
  next: '<path d="M5 5.5v13l9-6.5zM18 5v14"/>',
  prev: '<path d="M19 5.5v13l-9-6.5zM6 5v14"/>',
  volume: '<path d="M4 9.5v5h3.5L12 19V5L7.5 9.5z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/>',
  speak: '<path d="M4 4.5h16a1 1 0 0 1 1 1v10.5a1 1 0 0 1-1 1h-9l-5 4v-4H4a1 1 0 0 1-1-1V5.5a1 1 0 0 1 1-1z"/><path d="M8 9v3M11 8v5M14 9.5v2M17 9v3"/>',
  vibrate: '<rect x="8" y="4" width="8" height="16" rx="2"/><path d="M4.5 8v8M19.5 8v8M2 10v4M22 10v4"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  shrink: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M7.5 14h9"/>',
  send: '<path d="M4 12 20 4l-6 16-3-7z"/>',
  reply: '<path d="M9 7 4 12l5 5M4 12h10a6 6 0 0 1 6 6"/>',
  wifi: '<path d="M2 8.8a15 15 0 0 1 20 0"/><path d="M5.2 12.4a10 10 0 0 1 13.6 0"/><path d="M8.6 15.8a5 5 0 0 1 6.8 0"/><path d="M12 19.5h.01"/>',
  signal: '<path d="M4 20v-3M9 20v-7M14 20V9M19 20V4"/>',
  disk: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M5 13 7.5 5h9L19 13M7 16.5h.01"/>',
  memory: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 11v2M11 11v2M15 11v2M7 17v3M17 17v3"/>',
  thermo: '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  video: '<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="m15.5 10 6-3.5v11l-6-3.5"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="m21 15-4.5-4.5L6 21"/>',
  "in": '<path d="M17 7 7 17M7 9v8h8"/>',
  out: '<path d="M7 17 17 7M9 7h8v8"/>',
  missed: '<path d="M3.5 8l6.5 6.5 4-4 6.5 6.5"/><path d="M15 17h5.5v-5.5" />',
  ban: '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2.5"/><path d="M16 8V5.5A2.5 2.5 0 0 0 13.5 3h-8A2.5 2.5 0 0 0 3 5.5v8A2.5 2.5 0 0 0 5.5 16H8"/>',
  shield: '<path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6z"/>',
  key: '<circle cx="8" cy="15" r="4.5"/><path d="m11.5 11.5 8-8M16 7l3 3M18.5 4.5l2 2"/>',
};

export function icon(name, cls = "") {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", "0 0 24 24");
  el.setAttribute("class", ("i " + cls).trim());
  el.setAttribute("aria-hidden", "true");
  // static, trusted markup from the table above (never phone data)
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

/** A phone time in ms. The phone's times are Unix milliseconds (Android's own); values that look
 *  like seconds (< 1e11) are taken as seconds. */
export function toMs(t) {
  if (t === null || t === undefined || t === "") return null;
  const n = Number(t);
  if (isNaN(n)) { const d = Date.parse(t); return isNaN(d) ? null : d; }
  return n < 1e11 ? n * 1000 : n;
}

export function fmtAgo(t) {
  const ms = toMs(t);
  if (ms === null) return "";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  if (s < 7 * 86400) return Math.floor(s / 86400) + " d ago";
  return new Date(ms).toLocaleDateString();
}

/** Today: "14:05"; this year: "3 Oct, 14:05"; else "3 Oct 2023". */
export function fmtWhen(t) {
  const ms = toMs(t);
  if (ms === null) return "";
  const d = new Date(ms), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return time;
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { day: "numeric", month: "short" }) + ", " + time;
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

/** battery.level as a percent (the phone sends 0..100; a fraction 0..1 is accepted too). */
export function batteryPct(level) {
  const n = Number(level);
  return Math.round(n > 0 && n < 1 ? n * 100 : n);
}

export function signalBars(level, max = 4) {
  const n = Math.max(0, Math.min(4, Math.round((level / max) * 4)));
  return h("span.signal-bars", { title: `${level} / ${max}` }, [1, 2, 3, 4].map((i) => h("i", { class: i <= n ? "on" : "" })));
}

// ------------------------------------------------------------- toolkit
export function toast(msg, kind = "") {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = h("div.toast", { class: kind, role: kind === "err" ? "alert" : "status" },
               kind === "err" ? icon("alert") : kind === "ok" ? icon("check") : null, h("span", null, msg));
  box.append(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 260); }, kind === "err" ? 5600 : 3200);
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

export function sheet({ title, body, foot, wide = false, onClose, cls = "" } = {}) {
  const overlay = h("div.overlay");
  const bodyEl = h("div.sheet-body", null, body);
  const footEl = foot ? h("div.sheet-foot", null, foot) : null;
  const titleEl = h("h2.ellipsis", null, title || "");
  const panel = h("div.sheet", { class: [wide ? "wide" : "", cls].join(" ").trim(), role: "dialog", "aria-modal": "true",
                                 "aria-label": title || "" },
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
      onClose: () => resolve(result), cls: "confirm",
    });
    setTimeout(() => okBtn.focus(), 50);
  });
}

/** A small form in a sheet. fields: [{name, label, type, value, placeholder, hint, rows}] → values or null. */
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
        const el = f.rows
          ? h("textarea.input.area", { name: f.name, rows: f.rows, placeholder: f.placeholder || "", spellcheck: "true" }, f.value || "")
          : h("input.input", { type: f.type || "text", name: f.name, value: f.value || "", placeholder: f.placeholder || "",
                               autocomplete: f.autocomplete || "off", spellcheck: "false", inputmode: f.inputmode });
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

export function seg(options, value, onChange, { full = false, label } = {}) {
  const el = h("div.seg", { class: full ? "full" : "", role: "radiogroup", "aria-label": label || "" });
  let cur = value;
  const render = (v) => {
    cur = v;
    clear(el, options.map(([val, text]) => h("button", {
      type: "button", class: val === v ? "active" : "", role: "radio", "data-value": String(val),
      "aria-checked": String(val === v), onclick: () => { if (val !== cur) { render(val); onChange(val); } },
    }, text)));
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
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(done, () => toast("Copy failed", "err"));
  return Promise.resolve();
}

export function emptyState(ic, title, text) {
  return h("div.empty", null, icon(ic), h("b", null, title), text ? h("div", null, text) : null);
}

export function spinnerBlock(text = "Loading…") {
  return h("div.empty.loading", null, h("span.spin"), h("div", null, text));
}

export function debounce(fn, ms) {
  let t = 0;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}

// ------------------------------------------------------------------ URLs
/** An http(s) URL of the phone, relative to this page's directory. */
export function url(rel) {
  const u = new URL(rel, location.href);
  u.hash = "";
  return u.href;
}

/** A ws(s) URL of the phone, relative to this page's directory (the viewer's shim carries it). */
export function wsUrl(rel) {
  const u = new URL(rel, location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.hash = "";
  return u.href;
}

export function q(params) {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") s.set(k, String(v));
  return s.toString();
}

// ------------------------------------------------------------------- ops
export class PhoneError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

function httpError(status, body, what) {
  if (body && body.error) return new PhoneError(body.error, status);
  if (status === 401) return new PhoneError("The access key expired in this browser - open the phone again from the device list.", 401);
  if (status === 502 || status === 503 || status === 504) return new PhoneError("The phone is not reachable right now.", status);
  return new PhoneError(`${what} failed (HTTP ${status})`, status);
}

/** POST api/op/<name> → data; throws PhoneError with the phone's sentence. */
export async function op(name, args = {}, { timeout = 20000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  let r;
  try {
    r = await fetch("api/op/" + name, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args || {}),
      cache: "no-store", signal: ctl.signal,
    });
  } catch (e) {
    throw new PhoneError(e && e.name === "AbortError" ? `The phone did not answer (${name})` : "The phone is not reachable right now.", 0);
  } finally {
    clearTimeout(timer);
  }
  let d = null;
  try { d = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok || !d || d.ok === false) throw httpError(r.status, d, name);
  return d.data;
}

/** GET a JSON route (api/info, api/files/list). */
export async function getJson(rel, { timeout = 20000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  let r;
  try {
    r = await fetch(rel, { cache: "no-store", signal: ctl.signal });
  } catch (e) {
    throw new PhoneError(e && e.name === "AbortError" ? "The phone did not answer" : "The phone is not reachable right now.", 0);
  } finally {
    clearTimeout(timer);
  }
  let d = null;
  try { d = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok || !d || d.ok === false) throw httpError(r.status, d, rel.split("?")[0]);
  if (d.ok === true && "data" in d && Object.keys(d).length === 2) return d.data;     // tolerate an op-style wrapper
  return d;
}

/** An op with a spinner on `btn`, errors as toasts, optional success toast; → data or undefined. */
export function run(name, args, { ok, btn, timeout } = {}) {
  return busy(btn, async () => {
    const r = await op(name, args, { timeout });
    if (ok) toast(ok, "ok");
    return r === undefined || r === null ? {} : r;      // ops without a result answer data: null
  });
}

// ----------------------------------------------------------------- store
/** Latest data of every retained topic of /ws (+ "info" and "features" from api/info) + subscribers. */
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

/** The feature record of /api/info (or the `features` topic): {id, title, on, ready, missing, note}. */
export function feature(id) {
  return (store.get("features") || []).find((f) => f.id === id) || null;
}

export function featureReady(id) {
  const f = feature(id);
  return !!(f && f.on && f.ready);
}

/** Why a feature can't be used, or "" when it can. */
export function featureReason(id) {
  const f = feature(id);
  if (!f) return "This phone does not have this feature.";
  if (!f.on) return `${f.title} is turned off on the phone.`;
  if (!f.ready) {
    const miss = (f.missing || []).map((m) => (typeof m === "string" ? m : m.name)).filter(Boolean);
    return `${f.title} needs ${miss.length ? miss.join(", ") : "a permission"} - allow it in the Arstro Remote Client app on the phone.`;
  }
  return "";
}

// ------------------------------------------------------------ events /ws
/** The phone's event socket: retained topics into `store`, one-off events to listeners; reconnects. */
class Events {
  constructor() {
    this.ws = null;
    this.status = "idle";             // idle | connecting | open | wait
    this.backoff = 800;
    this.handlers = { status: new Set(), event: new Set() };
    this.wanted = false;
    this.opened = 0;                  // connections opened so far (tests, reconnect logic)
  }

  on(what, fn) { this.handlers[what].add(fn); return () => this.handlers[what].delete(fn); }
  emit(what, ...args) {
    for (const fn of [...this.handlers[what]]) {
      try { fn(...args); } catch (e) { console.error(e); }
    }
  }
  setStatus(s) { if (this.status !== s) { this.status = s; this.emit("status", s); } }

  start() { this.wanted = true; if (!this.ws) this.open(); }

  open() {
    clearTimeout(this.retry);
    this.setStatus("connecting");
    let ws;
    try { ws = new WebSocket(wsUrl("ws")); } catch (e) { this.later(); return; }
    this.ws = ws;
    const watchdog = setTimeout(() => { if (this.ws === ws && ws.readyState !== 1) ws.close(); }, 15000);
    ws.onopen = () => {
      clearTimeout(watchdog);
      if (this.ws !== ws) return;
      this.opened++;
      this.backoff = 800;
      this.setStatus("open");
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return;
      let msg;
      try { msg = JSON.parse(e.data); } catch (x) { return; }
      if (msg.ev === "state" && msg.topic) {
        let data = msg.data;
        if (msg.topic === "features" && data && !Array.isArray(data) && Array.isArray(data.features)) data = data.features;
        store.set(msg.topic, data);
      } else if (msg.ev) this.emit("event", msg.ev, msg);
    };
    ws.onclose = () => {
      clearTimeout(watchdog);
      if (this.ws !== ws) return;
      this.ws = null;
      if (!this.wanted) { this.setStatus("idle"); return; }
      this.later();
    };
  }

  later() {
    this.setStatus("wait");
    this.retry = setTimeout(() => this.wanted && this.open(), this.backoff);
    this.backoff = Math.min(this.backoff * 2, 10000);
  }

  /** Reconnect now (the page became visible again). */
  kick() {
    if (this.wanted && !this.ws) { this.backoff = 800; this.open(); }
  }
}

export const events = new Events();

/** Listen for one-off /ws events of a name ("sms", "notification"). Returns an unsubscribe. */
export function onEvent(name, fn) {
  return events.on("event", (ev, msg) => { if (ev === name) fn(msg); });
}

// ---------------------------------------------------------------- misc
export function isNarrow() { return window.matchMedia("(max-width: 899px)").matches; }

export function initials(name) {
  const s = String(name || "").trim();
  if (!s) return "#";
  if (/^[+\d\s()-]+$/.test(s)) return "#";
  const parts = s.split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || "") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

/** Compare phone numbers loosely (the last 9 digits). */
export function sameNumber(a, b) {
  const d = (x) => String(x || "").replace(/\D/g, "").slice(-9);
  const da = d(a), db = d(b);
  return da.length >= 3 ? da === db : String(a || "") === String(b || "");
}

/** A tiny per-browser preference (localStorage may be missing: private mode, previews). */
export const pref = {
  get(k, dflt = null) { try { const v = localStorage.getItem("phone." + k); return v === null ? dflt : JSON.parse(v); } catch (e) { return dflt; } },
  set(k, v) { try { localStorage.setItem("phone." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

window.phoneDebug = window.phoneDebug || {};
window.phoneDebug.store = store;
window.phoneDebug.events = events;
