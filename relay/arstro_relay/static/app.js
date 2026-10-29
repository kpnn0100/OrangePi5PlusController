/* The device manager's device list (RLY-21, RLY-22): the devices that advertise here, and for one
 * device: its access key (kept in this browser as a non-extractable key, remembered or for 12 h),
 * its listening ports (through the end-to-end channel), and "Open" for B/d/<id>/<port>/.
 * No frameworks, no inline scripts (the page's CSP is script-src 'self'). */
(function () {
  "use strict";

  const V = window.ArstroViewer;
  const E = window.ArstroE2E;
  const B = V.baseOf(new URL(document.currentScript.src).pathname, "static/app.js");
  const POLL = 5000;
  const SECURE = !!(window.isSecureContext && window.crypto && window.crypto.subtle && window.indexedDB);

  const $ = (id) => document.getElementById(id);
  const main = $("main");

  let info = null;
  let devices = [];
  let pollTimer = null;
  let current = null;           // the open device view: {id, els..., busy}

  // ------------------------------------------------------------------ DOM helper
  function h(tag, attrs) {
    const [name, ...classes] = tag.split(".");
    const el = document.createElement(name || "div");
    if (classes.length) el.className = classes.join(" ");
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else if (k in el && typeof v !== "string") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { c.forEach((x) => add(el, x)); return; }
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  function clear(el, ...children) {
    el.replaceChildren();
    children.forEach((c) => add(el, c));
  }

  // ------------------------------------------------------------------ formatting
  function toMs(t) {
    if (t === null || t === undefined || t === "") return null;
    if (typeof t === "number") return t < 1e12 ? t * 1000 : t;
    const n = Date.parse(t);
    return isNaN(n) ? null : n;
  }
  function ago(t) {
    const ms = toMs(t);
    if (ms === null) return "never";
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 10) return "just now";
    if (s < 60) return s + " s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    if (s < 30 * 86400) return Math.round(s / 86400) + " d ago";
    return new Date(ms).toLocaleDateString();
  }
  function clock(ms) {
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  // ------------------------------------------------------------------ server
  async function api(path, opts) {
    const r = await fetch(B + "/api/" + path, Object.assign({ cache: "no-store", credentials: "same-origin" }, opts || {}));
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok) {
      const err = new Error((body && body.error) || ("HTTP " + r.status));
      err.status = r.status;
      throw err;
    }
    return body;
  }

  function banner(text) {
    const b = $("banner");
    b.textContent = text || "";
    b.hidden = !text;
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    if (!SECURE) {
      banner("Opening devices needs a secure connection: use https:// (or http://localhost). The list works, " +
             "but keys and pages do not - use the agent / CLI client instead.");
    } else if (navigator.serviceWorker) {
      // install the viewer's service worker now, so "Open" lands on the device page directly
      navigator.serviceWorker.register(B + "/sw.js", { scope: B + "/" }).catch((e) => {
        banner("The viewer could not start its service worker: " + e.message);
      });
    }
    try {
      info = await api("info");
    } catch (e) {
      clear(main, h("div.empty", { text: "The device manager is not reachable: " + e.message }));
      return;
    }
    const name = info.name && info.name !== "Devices" ? info.name : "Arstro device manager";
    $("relay-name").textContent = [name, info.version ? "v" + info.version : ""].filter(Boolean).join(" · ");
    $("logout").hidden = info.auth !== "password" || !info.authorized;
    if (info.auth === "password" && !info.authorized) return showLogin();
    await refresh();
    route();
    schedule();
  }

  function schedule() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!document.hidden) await refresh();
      schedule();
    }, POLL);
  }

  async function refresh() {
    try {
      const d = await api("devices");
      devices = d.devices || [];
      if (!current) renderList();
      else updateDeviceHead();
    } catch (e) {
      if (e.status === 401) { clearTimeout(pollTimer); return showLogin(); }
      if (!current) clear(main, h("div.empty", { text: "Could not load the devices: " + e.message }));
    }
  }

  $("logout").addEventListener("click", async () => {
    await api("logout", { method: "POST" }).catch(() => {});
    location.hash = "";
    location.reload();
  });

  window.addEventListener("hashchange", route);

  function route() {
    const id = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
    if (id && V.ID_RE.test(id)) showDevice(id);
    else { current = null; renderList(); }
  }

  // ------------------------------------------------------------------ login
  function showLogin(message) {
    current = null;
    $("logout").hidden = true;
    const input = h("input.input", { type: "password", name: "password", autocomplete: "current-password",
                                     placeholder: "Password", "aria-label": "Password", required: true });
    const err = h("p.err", { role: "alert", text: message || "" });
    const btn = h("button.btn.primary", { type: "submit", text: "Sign in" });
    const form = h("form", { id: "login-form", onsubmit: async (e) => {
      e.preventDefault();
      btn.disabled = true;
      err.textContent = "";
      try {
        await api("login", { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify({ password: input.value }) });
        location.reload();
      } catch (x) {
        err.textContent = x.status === 401 ? "That password is not right." : "Could not sign in: " + x.message;
        btn.disabled = false;
        input.select();
      }
    } }, input, btn, err);
    clear(main, h("div.center", {}, h("div.card.narrow", {},
      h("h2", { text: "Sign in" }),
      h("p.note", { text: "This device manager is protected with a password." }), form)));
    input.focus();
  }

  // ------------------------------------------------------------------ the list
  function statusPill(d) {
    return h("span.pill." + (d.online ? "on" : "off"), { text: d.online ? "online" : "offline" });
  }

  function metaBits(d) {
    const bits = [];
    if (d.hostname) bits.push(d.hostname);
    if (d.platform) bits.push(d.platform);
    if (d.version) bits.push("v" + d.version + (d.slot ? " · slot " + d.slot : ""));
    bits.push(d.online ? (d.since ? "online for " + ago(d.since).replace(/ ago$/, "").replace("just now", "a moment") : "online")
                       : "last seen " + ago(d.last_seen));
    if (d.viewers) bits.push(plural(d.viewers, "viewer", "viewers"));
    return bits;
  }

  function renderList() {
    if (current || !info) return;
    if (!devices.length) {
      clear(main, h("div.section-head", {}, h("h2", { text: "Devices" })),
        h("div.empty", {},
          h("p", { text: "No device has advertised to this device manager yet." }),
          h("p.note", { text: "On a machine: install.sh --relay <this URL> --relay-token <join token>, or the " +
                              "Internet access page of Arstro Remote." })));
      return;
    }
    const online = devices.filter((d) => d.online).length;
    clear(main,
      h("div.section-head", {}, h("h2", { text: "Devices" }),
        h("span.count", { text: online + " of " + devices.length + " online" })),
      h("div.devices", { id: "devices" }, devices.map((d) =>
        h("button.device" + (d.online ? "" : ".offline"), {
          type: "button", "data-id": d.id, "aria-label": "Open " + d.name,
          onclick: () => { location.hash = "#" + encodeURIComponent(d.id); },
        },
        h("span.name", { text: d.name || d.id }), statusPill(d),
        h("span.meta", {}, metaBits(d).map((b) => h("span", { text: b })))))));
  }

  // ------------------------------------------------------------------ one device
  function deviceById(id) {
    return devices.find((d) => d.id === id) || null;
  }

  async function showDevice(id) {
    const d = deviceById(id) || { id, name: id, online: false };
    const els = {};
    current = { id, els, rec: null, ports: null };
    const c = current;

    els.head = h("div.dev-head");
    els.meta = h("div.dev-meta");
    els.keyBody = h("div");
    els.portInput = h("input.input.port.mono", { type: "number", min: "1", max: "65535", inputmode: "numeric",
                                                 id: "port", placeholder: "port", "aria-label": "Port",
                                                 value: d.web ? String(d.web) : "" });
    els.check = h("button.btn", { type: "button", id: "check", text: "Check", onclick: () => check(c) });
    els.open = h("button.btn.primary", { type: "button", id: "open", text: "Open", onclick: () => openPort(c) });
    els.ports = h("div.ports", { id: "ports" });
    els.portsNote = h("p.note", { id: "ports-note" });
    els.err = h("p.err", { id: "device-error", role: "alert" });
    els.ok = h("p.ok-text", { id: "device-ok" });
    els.link = h("p.open-link", { id: "open-link" });
    els.portInput.addEventListener("input", () => { updateLink(c); markPort(c); });
    els.portInput.addEventListener("keydown", (e) => { if (e.key === "Enter") openPort(c); });

    clear(main,
      h("button.btn.ghost.sm.back", { type: "button", text: "‹ All devices", onclick: () => { location.hash = ""; } }),
      els.head, els.meta,
      h("section.card", { "aria-labelledby": "key-h" }, h("h3", { id: "key-h", text: "Access key" }), els.keyBody),
      h("section.card", { "aria-labelledby": "port-h" }, h("h3", { id: "port-h", text: "Port" }),
        h("div.row", {}, els.portInput, els.open, els.check),
        els.link, els.portsNote, els.ports, els.err, els.ok));
    updateDeviceHead();
    updateLink(c);
    if (!SECURE) {
      els.keyBody.textContent = "Keys need a secure connection (https:// or http://localhost).";
      els.check.disabled = els.open.disabled = true;
      return;
    }
    await renderKey(c);
  }

  function updateDeviceHead() {
    if (!current) return;
    const d = deviceById(current.id) || { id: current.id, name: current.id, online: false };
    clear(current.els.head, h("h2", { text: d.name || d.id }), statusPill(d));
    clear(current.els.meta, h("span.mono", { text: d.id }), metaBits(d).map((b) => h("span", { text: b })));
    if (!current.els.portInput.value && d.web) { current.els.portInput.value = String(d.web); updateLink(current); }
  }

  function portValue(c) {
    const p = Number(c.els.portInput.value);
    return Number.isInteger(p) && p >= 1 && p <= 65535 ? p : null;
  }

  function pageUrl(id, port) {
    return B + "/d/" + id + "/" + port + "/";
  }

  function updateLink(c) {
    const p = portValue(c);
    if (!p) { c.els.link.textContent = ""; return; }
    clear(c.els.link, "Opens ", h("a", { href: pageUrl(c.id, p), target: "_blank", rel: "noopener", text: pageUrl(c.id, p) }));
  }

  async function renderKey(c) {
    let rec = null;
    try { rec = await V.getKey(c.id); } catch (e) { c.els.err.textContent = "This browser cannot store keys: " + e.message; }
    if (current !== c) return;
    c.rec = rec;
    if (rec) {
      const until = rec.remember ? "remembered on this browser" : "kept until " + clock(rec.expires);
      clear(c.els.keyBody, h("div.stored", { id: "key-stored" },
        "Key ", h("span.kid", { text: rec.key_id }), " stored · " + until + " · ",
        h("button.btn.link", { type: "button", id: "forget", text: "Forget", onclick: () => forget(c) })));
      c.els.keyInput = null;
      c.els.remember = null;
      return;
    }
    c.els.keyInput = h("input.input.mono.grow", { type: "password", id: "key", autocomplete: "off", spellcheck: "false",
                                                  placeholder: "ak1_…", "aria-label": "Access key" });
    c.els.remember = h("input", { type: "checkbox", id: "remember" });
    c.els.keyInput.addEventListener("keydown", (e) => { if (e.key === "Enter") check(c); });
    clear(c.els.keyBody,
      h("div.row", {}, c.els.keyInput),
      h("div.row", { style: "margin-top:10px" }, h("label.check", {}, c.els.remember, "Remember on this browser")),
      h("p.note", { text: "The key is shown on the device: the Internet access page of Arstro Remote, or " +
                          "arstro-remote relay key. Not remembered, it is kept for 12 hours. It never leaves this " +
                          "browser: it is stored as a key the page itself cannot read back." }));
  }

  /** The key to use: the typed one (-> {key, key_id, typed: true}) or the stored record. */
  async function chosenKey(c) {
    const text = c.els.keyInput ? c.els.keyInput.value.trim() : "";
    if (text) {
      const k = await E.keyFromText(text);     // throws with a readable message
      return { key: k.key, key_id: k.key_id, typed: true };
    }
    if (c.rec) return { key: c.rec.key, key_id: c.rec.key_id, typed: false };
    throw new Error("Enter the device's access key first.");
  }

  async function store(c, k) {
    const remember = !!(c.els.remember && c.els.remember.checked);
    await V.putKey(V.keyRecord(c.id, k.key, k.key_id, remember));
    tellWorker({ t: "forget", id: c.id });     // a channel with an older key is dropped
    await renderKey(c);
  }

  function setBusy(c, busy, what) {
    c.els.check.disabled = busy;
    c.els.open.disabled = busy;
    c.els.check.textContent = busy && what === "check" ? "Checking…" : "Check";
  }

  async function check(c) {
    c.els.err.textContent = "";
    c.els.ok.textContent = "";
    let k;
    try { k = await chosenKey(c); } catch (e) { c.els.err.textContent = e.message; return; }
    setBusy(c, true, "check");
    let ch = null;
    try {
      ch = await V.openChannel(B, c.id, k.key);
      const ports = await ch.ports();
      if (current !== c) return;
      if (k.typed) await store(c, k);
      renderPorts(c, ports, ch.info && ch.info.ports);
      c.els.ok.textContent = "Key accepted by " + ((ch.info && (ch.info.name || ch.info.hostname)) || c.id) + ".";
    } catch (e) {
      if (current !== c) return;
      c.els.err.textContent = e instanceof E.WrongKey ? "wrong access key for this device" : V.errorText(e);
    } finally {
      if (ch) ch.close("done");
      if (current === c) setBusy(c, false);
    }
  }

  function renderPorts(c, ports, allowed) {
    const list = (ports || []).slice().sort((a, b) => a.port - b.port);
    const seen = new Set();
    const uniq = list.filter((p) => !seen.has(p.port) && seen.add(p.port));
    c.els.portsNote.textContent = uniq.length ? "Listening ports (pick one):" : "Nothing listens on a TCP port the device lets you reach.";
    if (Array.isArray(allowed)) c.els.portsNote.textContent += " Allowed on this device: " + (allowed.join(", ") || "none") + ".";
    clear(c.els.ports, uniq.map((p) => h("button.btn.sm.port-btn", {
      type: "button", "data-port": String(p.port), title: [p.addr, p.proc].filter(Boolean).join(" · "),
      onclick: () => { c.els.portInput.value = String(p.port); updateLink(c); markPort(c); },
    }, String(p.port), p.proc ? h("span.proc", { text: p.proc }) : null)));
    markPort(c);
  }

  function markPort(c) {
    const p = String(portValue(c) || "");
    for (const b of c.els.ports.children) b.classList.toggle("active", b.getAttribute("data-port") === p);
  }

  async function openPort(c) {
    c.els.err.textContent = "";
    const p = portValue(c);
    if (!p) { c.els.err.textContent = "Enter a port (1-65535)."; c.els.portInput.focus(); return; }
    if (!SECURE) return;
    // the window opens now, while the click still counts as a user action (popup blockers)
    const w = window.open("about:blank", "_blank");
    try {
      const text = c.els.keyInput ? c.els.keyInput.value.trim() : "";
      if (text) await store(c, Object.assign(await E.keyFromText(text), { typed: true }));
      else if (!c.rec) throw new Error("Enter the device's access key first.");
      await navigator.serviceWorker.register(B + "/sw.js", { scope: B + "/" }).catch(() => null);
      const url = pageUrl(c.id, p);
      if (w) { try { w.opener = null; } catch (e) { /* cross-origin already */ } w.location.href = url; }
      else location.href = url;               // the popup was blocked: open it here
    } catch (e) {
      if (w) w.close();
      c.els.err.textContent = V.errorText(e);
    }
  }

  async function forget(c) {
    try {
      await V.deleteKey(c.id);
      await V.clearJars(c.id).catch(() => {});
      tellWorker({ t: "forget", id: c.id });
    } catch (e) {
      c.els.err.textContent = V.errorText(e);
    }
    clear(c.els.ports);
    c.els.portsNote.textContent = "";
    c.els.ok.textContent = "Key forgotten (and the device's sign-ins in this browser).";
    await renderKey(c);
  }

  function tellWorker(msg) {
    const sw = navigator.serviceWorker && navigator.serviceWorker.controller;
    if (sw) sw.postMessage(msg);
  }

  boot();
})();
