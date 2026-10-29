/* The boot page (RLY-22): the relay serves it for B/d/<id>/<port>/... whenever the viewer's
 * service worker did not answer that URL - the first visit (no worker yet), no key in this
 * browser, or the worker could not reach the device. It installs B/sw.js (scope B/), asks for
 * the key when there is none, says what went wrong otherwise, and reloads into the device page. */
(function () {
  "use strict";

  const V = window.ArstroViewer;
  const E = window.ArstroE2E;
  const B = V.baseOf(new URL(document.currentScript.src).pathname, "static/boot.js");
  const dev = V.parseDevicePath(location.pathname, B);
  const $ = (id) => document.getElementById(id);
  const RELOADS = "arstro-boot-reloads";
  // a transport forced for this tab (ArstroE2E.transport from sessionStorage, tests) is the worker's too
  const SW_URL = B + "/sw.js" + (E.transport && E.transport !== "auto" ? "?transport=" + encodeURIComponent(E.transport) : "");

  function status(text) {
    $("status").hidden = !text;
    $("status-text").textContent = text || "";
  }
  function error(text) {
    status("");
    $("error").textContent = text || "";
  }

  function reloads() {
    try {
      const r = JSON.parse(sessionStorage.getItem(RELOADS) || "null");
      return r && r.path === location.pathname && Date.now() - r.at < 15000 ? r.n : 0;
    } catch (e) { return 0; }
  }
  function reload() {
    try { sessionStorage.setItem(RELOADS, JSON.stringify({ path: location.pathname, at: Date.now(), n: reloads() + 1 })); } catch (e) { /* private mode */ }
    location.reload();
  }
  function resetReloads() {
    try { sessionStorage.removeItem(RELOADS); } catch (e) { /* private mode */ }
  }

  /** Why the worker could not show the page (it remembers the last failure per device and port). */
  function askWorker() {
    const sw = navigator.serviceWorker.controller;
    if (!sw) return Promise.resolve(null);
    return new Promise((resolve) => {
      const mc = new MessageChannel();
      const t = setTimeout(() => resolve(null), 1500);
      mc.port1.onmessage = (e) => { clearTimeout(t); resolve((e.data && e.data.error) || null); };
      sw.postMessage({ t: "why", id: dev.id, port: dev.port }, [mc.port2]);
    });
  }

  function showKeyForm(message) {
    error(message || "");
    if (!message) status("");
    $("actions").hidden = true;
    $("key-form").hidden = false;
    $("key").focus();
  }

  $("key-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("key-submit");
    error("");
    let k;
    try { k = await E.keyFromText($("key").value); } catch (x) { error(x.message); return; }
    btn.disabled = true;
    status("Checking the key with the device…");
    let ch = null;
    try {
      ch = await V.openChannel(B, dev.id, k.key);
    } catch (x) {
      btn.disabled = false;
      error(x instanceof E.WrongKey ? "wrong access key for this device" : V.errorText(x));
      return;
    } finally {
      if (ch) ch.close("checked");
    }
    await V.putKey(V.keyRecord(dev.id, k.key, k.key_id, $("remember").checked));
    const sw = navigator.serviceWorker.controller;
    if (sw) sw.postMessage({ t: "forget", id: dev.id });
    status("Opening…");
    resetReloads();
    reload();
  });
  $("retry").addEventListener("click", () => { resetReloads(); location.reload(); });
  $("other-key").addEventListener("click", () => showKeyForm(""));

  async function main() {
    if (!dev) { error("This is not a device address (expected " + B + "/d/<device>/<port>/)."); return; }
    $("where").textContent = dev.id + " · port " + dev.port;
    $("title").textContent = "Opening " + dev.id;
    if (!window.isSecureContext || !navigator.serviceWorker || !window.crypto || !window.crypto.subtle) {
      error("Opening a device needs a secure connection: use https:// (or http://localhost). Browsers allow the " +
            "service worker and the encryption this needs only there - for plain http use the agent / CLI client " +
            "(port forward) instead.");
      return;
    }
    let info = null;
    try { info = await (await fetch(B + "/api/info", { cache: "no-store" })).json(); } catch (e) { info = null; }
    if (info && info.auth === "password" && !info.authorized) {
      error("Sign in to the device manager first, then open the device again.");
      return;
    }
    status("Starting the viewer…");
    try {
      await navigator.serviceWorker.register(SW_URL, { scope: B + "/" });
      await navigator.serviceWorker.ready;
    } catch (e) {
      error("The viewer could not start its service worker: " + e.message);
      return;
    }
    let rec = null;
    try { rec = await V.getKey(dev.id); } catch (e) {
      error("This browser cannot store the key (" + e.message + "). A private window may not allow it.");
      return;
    }
    if (!rec) { showKeyForm(""); return; }
    if (!navigator.serviceWorker.controller) {
      if (reloads() < 3) { status("Opening…"); reload(); return; }
      error("The viewer's service worker did not take over this page. Reload it (a forced reload, Shift+Reload, " +
            "skips the service worker).");
      $("actions").hidden = false;
      return;
    }
    // the worker is in control and has a key, yet this page came from the network: it could not
    // get the device's page. Find out why.
    resetReloads();
    status("Checking the connection to the device…");
    let why = await askWorker();
    let wrongKey = false;
    let ch = null;
    try {
      ch = await V.openChannel(B, dev.id, rec);
      const allowed = ch.info && ch.info.ports;
      if (Array.isArray(allowed) && !allowed.includes(dev.port)) why = "port " + dev.port + " is not allowed on this device";
    } catch (e) {
      why = e instanceof E.WrongKey ? "wrong access key for this device" : V.errorText(e);
      wrongKey = e instanceof E.WrongKey;
    } finally {
      if (ch) ch.close("checked");
    }
    if (wrongKey) { showKeyForm("The stored key is not this device's key (wrong access key for this device). Enter the right one."); return; }
    error(why ? "Could not open the page: " + why : "The device did not deliver this page. Try again.");
    $("actions").hidden = false;
  }

  main().catch((e) => error(V.errorText(e)));
})();
