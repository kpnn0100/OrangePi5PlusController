// Wi-Fi (WIFI-01..07): status, radio, scan, connect (saved / new / hidden), disconnect, forget.

import { h, clear, icon, store, conn, run, formBox, confirmBox, toggle, signalBars, toast, errText } from "../core.js";

export default {
  id: "wifi", title: "Wi-Fi", icon: "wifi",

  mount(root) {
    let networks = null;
    let saved = [];
    let scanning = false;

    const statusCard = h("div.card");
    const scanBtn = h("button.btn.sm", { onclick: () => scan(true) }, icon("refresh"), "Scan");
    const hiddenBtn = h("button.btn.sm.ghost", { onclick: () => joinHidden() }, icon("hidden"), "Hidden network");
    const netList = h("div.list");
    const savedList = h("div.list");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Wi-Fi"), h("div.sub", null, "The Pi's wireless connection"))),
      statusCard,
      h("div.section", null, h("div.section-title", null, "Networks", h("div.actions", null, hiddenBtn, scanBtn)),
        h("div.card", { style: { padding: "6px 14px" } }, netList)),
      h("div.section", null, h("div.section-title", null, "Saved"),
        h("div.card", { style: { padding: "6px 14px" } }, savedList)));

    function renderStatus(w) {
      w = w || {};
      const radio = toggle(w.enabled, (on) => run("wifi.radio", { enabled: on }, { ok: on ? "Wi-Fi on" : "Wi-Fi off" }),
                           { label: "Wi-Fi radio" });
      clear(statusCard,
        h("div.row", null,
          h("div.ico", { style: { width: "44px", height: "44px", borderRadius: "13px", display: "grid", placeItems: "center",
                                  background: w.connected ? "var(--ok-soft)" : "var(--card-3)",
                                  color: w.connected ? "var(--ok)" : "var(--muted)" } }, icon(w.enabled ? "wifi" : "wifi-off")),
          h("div.grow", null,
            h("div", { style: { fontWeight: 600, fontSize: "16px" } }, w.connected ? w.ssid : w.enabled ? "Not connected" : "Wi-Fi is off"),
            h("div.muted", { style: { fontSize: "13px" } },
              [w.device, w.connected ? w.ip : w.state, w.connected && w.signal ? w.signal + "%" : null].filter(Boolean).join(" · "))),
          w.connected ? h("button.btn.sm", { onclick: (e) => disconnect(e.currentTarget) }, "Disconnect") : null,
          radio));
    }

    function renderNetworks() {
      if (!networks) {
        clear(netList, h("div.empty", null, h("span.spin", { style: { margin: "0 auto" } })));
        return;
      }
      if (!networks.length) {
        clear(netList, h("div.empty", null, "No networks found."));
        return;
      }
      clear(netList, networks.map((n) => h("div.list-item.click", { onclick: () => connect(n) },
        h("div.ico", null, n.security && n.security !== "--" ? icon("lock") : icon("wifi")),
        h("div.grow", null,
          h("div.title.ellipsis", null, n.ssid || "(hidden)"),
          h("div.meta", null, [n.security && n.security !== "--" ? n.security : "Open", n.freq, n.saved ? "saved" : null].filter(Boolean).join(" · "))),
        n.in_use ? h("span.tag.ok", null, "Connected") : null,
        signalBars(n.signal))));
    }

    function renderSaved() {
      if (!saved.length) {
        clear(savedList, h("div.empty", null, "No saved networks."));
        return;
      }
      clear(savedList, saved.map((n) => h("div.list-item", null,
        h("div.ico", null, icon("wifi")),
        h("div.grow", null, h("div.title.ellipsis", null, n.ssid || n.name),
          h("div.meta", null, n.name !== n.ssid ? n.name : n.active ? "active" : "")),
        h("button.btn.sm.ghost", { onclick: () => connect({ ssid: n.ssid, saved: true }) }, "Connect"),
        h("button.btn.sm.ghost.danger", { onclick: () => forget(n) }, "Forget"))));
    }

    async function scan(rescan) {
      if (scanning) return;
      scanning = true;
      scanBtn.disabled = true;
      try {
        const [r, s] = await Promise.all([conn.call("wifi.scan", { rescan }, 60000), conn.call("wifi.saved")]);
        networks = r.networks || [];
        saved = s.networks || [];
      } catch (e) {
        toast(errText(e), "err");
        networks = networks || [];
      } finally {
        scanning = false;
        scanBtn.disabled = false;
        renderNetworks();
        renderSaved();
      }
    }

    async function connect(n) {
      let password = null;
      const secured = n.security && n.security !== "--";
      if (secured && !n.saved) {
        const v = await formBox({ title: "Join " + n.ssid, ok: "Connect",
          fields: [{ name: "password", label: "Password", type: "password", autocomplete: "new-password" }] });
        if (!v) return;
        password = v.password;
      }
      toast("Connecting to " + n.ssid + "…");
      const r = await run("wifi.connect", { ssid: n.ssid, password }, {});
      if (r) { toast("Connected to " + n.ssid, "ok"); scan(false); }
    }

    async function joinHidden() {
      const v = await formBox({ title: "Hidden network", ok: "Connect",
        fields: [{ name: "ssid", label: "Network name (SSID)" },
                 { name: "password", label: "Password", type: "password", hint: "Leave empty for an open network", autocomplete: "new-password" }] });
      if (!v || !v.ssid) return;
      toast("Connecting to " + v.ssid + "…");
      const r = await run("wifi.connect", { ssid: v.ssid, password: v.password || null, hidden: true });
      if (r) { toast("Connected to " + v.ssid, "ok"); scan(false); }
    }

    async function disconnect(btn) {
      const ok = await confirmBox({ title: "Disconnect Wi-Fi?", ok: "Disconnect", danger: true,
        text: "If you reach the Pi over this Wi-Fi, this page and the phone's media link will lose it. Bluetooth keeps working." });
      if (ok) await run("wifi.disconnect", {}, { ok: "Disconnected", btn });
    }

    async function forget(n) {
      const ok = await confirmBox({ title: "Forget " + (n.ssid || n.name) + "?", ok: "Forget", danger: true,
                                    text: "The saved password is removed from the Pi." });
      if (ok && await run("wifi.forget", { uuid: n.uuid }, { ok: "Forgotten" })) scan(false);
    }

    const off = store.on("wifi", renderStatus);
    if (!store.get("wifi")) renderStatus({});
    renderNetworks();
    renderSaved();
    const start = () => scan(false);
    const offReady = conn.on("ready", start);
    if (conn.status === "open") start();
    return () => { off(); offReady(); };
  },
};

