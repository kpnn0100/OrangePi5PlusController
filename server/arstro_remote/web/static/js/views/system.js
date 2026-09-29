// System (ADM-01..03, CON-05): server, connected controllers, Bluetooth pairing, web access.

import { h, clear, icon, store, conn, run, confirmBox, formBox, toggle, copyText, toast, fmtUptime, fmtAgo, CONTROLLER_NAMES, errText } from "../core.js";
import { login } from "../login.js";

const CTRL_ICON = { app: "phone", web: "globe", cli: "cmd", remote: "cmd", local: "cmd" };

export default {
  id: "system", title: "System", icon: "system",

  mount(root, { logout }) {
    let status = null;
    let web = null;
    let reveal = false;
    let pairing = store.get("pairing") || {};
    let pairAt = performance.now();

    const serverCard = h("div.card");
    const ctrlCard = h("div.card");
    const btCard = h("div.card");
    const webCard = h("div.card");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "System"), h("div.sub", null, "Server, controllers and access")),
        h("div.actions", null, h("button.btn.sm", { onclick: () => logout() }, icon("logout"), "Sign out"))),
      h("div.grid.two", null, serverCard, ctrlCard, btCard, webCard));

    function renderServer() {
      const s = status || {};
      const hello = conn.hello || {};
      clear(serverCard,
        h("div.card-title", null, icon("power"), "Server"),
        h("dl.kv", { style: { margin: 0 } },
          h("dt", null, "Name"), h("dd", null, hello.name || "Arstro Remote"),
          h("dt", null, "Version"), h("dd", null, s.version || hello.version || "–"),
          h("dt", null, "Host"), h("dd", null, (s.hostname || hello.hostname || "–") + (hello.user ? " · " + hello.user : "")),
          h("dt", null, "Running for"), h("dd", null, s.uptime !== undefined ? fmtUptime(s.uptime) : "–"),
          h("dt", null, "Input"), h("dd", null, hello.input ? (hello.input.available ? hello.input.backend : "unavailable") : "–"),
          h("dt", null, "Recorder"), h("dd", null, (store.get("recorder") || {}).available ? "running" : "not running")));
    }

    function renderControllers(list) {
      list = list || [];
      const me = conn.hello && conn.hello.session;
      clear(ctrlCard,
        h("div.card-title", null, icon("users"), "Connected controllers", h("span.tag", null, String(list.length))),
        list.length ? h("div.list", null, list.map((c) => h("div.list-item", null,
          h("div.ico", null, icon(CTRL_ICON[c.controller] || "cmd")),
          h("div.grow", null,
            h("div.title", null, CONTROLLER_NAMES[c.controller] || c.controller, c.session === me ? h("span.tag.accent", { style: { marginLeft: "8px" } }, "this page") : null),
            h("div.meta.ellipsis", null, [c.kind, c.peer, c.since ? "since " + fmtAgo(c.since) : null].filter(Boolean).join(" · "))))))
          : h("div.empty", null, "No controllers."));
    }

    function renderBt() {
      const p = pairing || {};
      const left = Math.max(0, Math.round((p.pairing_remaining || 0) - (performance.now() - pairAt) / 1000));
      const open = p.pairing_open && left > 0;
      clear(btCard,
        h("div.card-title", null, icon("bluetooth"), "Bluetooth"),
        !p.ready ? h("div.muted", null, "Bluetooth is not ready.") : h("div.stack", null,
          h("div.row", null,
            h("div.grow", null,
              h("div", { style: { fontWeight: 600 } }, p.alias || "–"),
              h("div.muted.mono", { style: { fontSize: "12.5px" } }, p.address || "")),
            open ? h("span.tag.ok.live", null, h("span.dot"), `Pairing ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`)
                 : h("span.tag", null, "Pairing closed")),
          h("div.row.wrap", null,
            h("button.btn.sm", { class: open ? "" : "primary",
                                 onclick: (e) => run("admin.pair", { seconds: 600 }, { ok: "Pairing open for 10 min", btn: e.currentTarget }) },
              open ? "Extend to 10 min" : "Open pairing (10 min)"),
            open ? h("button.btn.sm.ghost", { onclick: (e) => run("admin.pair", { seconds: 0 }, { ok: "Pairing closed", btn: e.currentTarget }) }, "Close now") : null),
          h("div.hint", null, "New phones can pair only while pairing is open."),
          h("div.section-title", { style: { marginTop: "8px" } }, "Paired devices"),
          (p.paired_devices || []).length ? h("div.list", null, p.paired_devices.map((d) => h("div.list-item", null,
            h("div.ico", null, icon("phone")),
            h("div.grow", null, h("div.title.ellipsis", null, d.name || d.address),
              h("div.meta.mono", null, d.address + (d.connected ? " · connected" : ""))),
            h("button.btn.sm.ghost.danger", { onclick: () => unpair(d) }, "Forget"))))
            : h("div.muted", { style: { fontSize: "13px" } }, "None yet.")));
    }

    async function unpair(d) {
      const ok = await confirmBox({ title: `Forget ${d.name || d.address}?`, ok: "Forget", danger: true,
        text: "It must pair again (inside the pairing window) to connect." });
      if (ok) run("admin.unpair", { address: d.address }, { ok: "Device forgotten" });
    }

    function renderWeb() {
      const w = { ...(web || {}), ...(store.get("web") || {}), token: (web || {}).token };
      const token = w.token || "";
      const open = w.auth === "open";
      const req = toggle(!open, (on) => setAuth(on), { label: "Require a password" });
      clear(webCard,
        h("div.card-title", null, icon("globe"), "Web & remote access"),
        h("div.stack", null,
          h("div", null, h("div.label", { style: { marginBottom: "6px" } }, "Addresses"),
            (w.urls || []).length ? (w.urls || []).map((u) => h("div.row", { style: { marginBottom: "4px" } },
              h("a.mono.grow.ellipsis", { href: u, target: "_blank", rel: "noopener" }, u),
              h("button.btn.ghost.icon.sm", { "aria-label": "Copy address", onclick: () => copyText(u) }, icon("copy"))))
              : h("div.muted", null, "No network address.")),
          h("div.row", null,
            h("div.grow", null, h("b", null, "Require a password"),
              h("div.hint", null, open ? "Off: anyone on this network can use the Pi, including its shell."
                                       : "Browsers ask once and remember it.")),
            req),
          open ? null : h("div", null, h("div.label", { style: { marginBottom: "6px" } }, "Password"),
            h("div.row", null,
              h("span.mono.grow.ellipsis", { style: { fontSize: "13px" } }, reveal ? token : token ? "•".repeat(14) : "–"),
              h("button.btn.ghost.icon.sm", { "aria-label": reveal ? "Hide password" : "Show password", onclick: () => { reveal = !reveal; renderWeb(); } }, icon("eye")),
              h("button.btn.ghost.icon.sm", { "aria-label": "Copy password", onclick: () => copyText(token) }, icon("copy")))),
          w.app ? h("div.row", null,
            h("div.grow", null, h("b", null, "Android app"), h("div.hint", null,
              `v${w.app.version || "?"} · open this page on the phone and tap Download to install or update`)),
            h("a.btn.sm", { href: w.app.path, download: "" }, icon("download"), "Download")) : null,
          h("div.hint", null, "Remote CLI: ", h("span.mono", null,
            `arstro-remote --url ${(w.urls || ["http://<pi>:" + (w.port || 8080) + "/"])[0]}` + (open ? "" : " --token …"))),
          open ? null : h("div.row", null, h("button.btn.sm", { onclick: () => changePassword() }, icon("key"), "Change password"))));
    }

    async function relogin(info) {
      if (info && info.auth !== "open" && info.token) await login(info.token);
      web = info;
      renderWeb();
    }

    async function setAuth(required) {
      if (!required) {
        const ok = await confirmBox({ title: "Turn the password off?", ok: "Turn off", danger: true,
          text: "Anyone on this network could then open a shell on the Pi, record, delete recordings and change Wi-Fi." });
        if (!ok) { renderWeb(); return; }
      }
      try {
        await relogin(await conn.call("web.set_auth", { required }));
        toast(required ? "Password required" : "Open access", "ok");
      } catch (e) {
        toast(errText(e), "err");
        renderWeb();
      }
    }

    async function changePassword() {
      const v = await formBox({ title: "Change password", ok: "Save",
        text: "Other browsers and remote CLIs must sign in again. The app picks it up by itself.",
        fields: [{ name: "pw", label: "New password", type: "password", autocomplete: "new-password", hint: "At least 8 characters" },
                 { name: "pw2", label: "Again", type: "password", autocomplete: "new-password" }] });
      if (!v) return;
      if (v.pw !== v.pw2) return toast("The passwords differ", "err");
      try {
        await relogin(await conn.call("web.set_password", { password: v.pw }));
        toast("Password changed", "ok");
      } catch (e) {
        toast(errText(e), "err");
      }
    }

    async function load() {
      try {
        const [s, w] = await Promise.all([conn.call("admin.status"), conn.call("web.info")]);
        status = s;
        web = w;
        if (s.bluetooth) { pairing = { ...pairing, ...s.bluetooth }; pairAt = performance.now(); }
      } catch (e) {
        toast(errText(e), "err");
      }
      renderServer();
      renderBt();
      renderWeb();
    }

    renderServer();
    renderBt();
    renderWeb();
    const offs = [
      store.on("controllers", renderControllers),
      store.on("pairing", (p) => { pairing = p; pairAt = performance.now(); renderBt(); }),
      store.on("recorder", () => renderServer(), { now: false }),
      store.on("web", () => renderWeb(), { now: false }),
      conn.on("ready", load),
    ];
    const timer = setInterval(() => { if (pairing.pairing_open) renderBt(); }, 1000);
    if (conn.status === "open") load();
    return () => { offs.forEach((f) => f()); clearInterval(timer); };
  },
};
