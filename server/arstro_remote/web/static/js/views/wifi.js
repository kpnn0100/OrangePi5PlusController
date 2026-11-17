// Wi-Fi (WIFI-01..08): status, radio, scan, connect (saved / new / hidden), disconnect, forget, and the
// machine's own network (hotspot).

import { h, clear, icon, store, conn, run, formBox, confirmBox, toggle, signalBars, toast, errText, sheet, seg,
         copyText } from "../core.js";

export default {
  id: "wifi", title: "Wi-Fi", icon: "wifi",

  mount(root) {
    let networks = null;
    let saved = [];
    let scanning = false;

    const statusCard = h("div.card");
    const hotspotCard = h("div.card");
    let hotspot = null;          // wifi.hotspot: settings + password (asked for, never pushed)
    let showPw = false;
    const scanBtn = h("button.btn.sm", { onclick: () => scan(true) }, icon("refresh"), "Scan");
    const hiddenBtn = h("button.btn.sm.ghost", { onclick: () => joinHidden() }, icon("hidden"), "Hidden network");
    const netList = h("div.list");
    const savedList = h("div.list");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Wi-Fi"), h("div.sub", null, "The Pi's wireless connection"))),
      statusCard,
      h("div.section", null, h("div.section-title", null, "Hotspot"), hotspotCard),
      h("div.section", null, h("div.section-title", null, "Networks", h("div.actions", null, hiddenBtn, scanBtn)),
        h("div.card", { style: { padding: "6px 14px" } }, netList)),
      h("div.section", null, h("div.section-title", null, "Saved"),
        h("div.card", { style: { padding: "6px 14px" } }, savedList)));

    function renderStatus(w) {
      w = w || {};
      const hs = w.hotspot && w.hotspot.active ? w.hotspot : null;
      const radio = toggle(w.enabled, (on) => run("wifi.radio", { enabled: on }, { ok: on ? "Wi-Fi on" : "Wi-Fi off" }),
                           { label: "Wi-Fi radio" });
      clear(statusCard,
        h("div.row", null,
          h("div.ico", { style: { width: "44px", height: "44px", borderRadius: "var(--r)", display: "grid", placeItems: "center",
                                  background: w.connected ? "var(--ok-soft)" : "var(--card-3)",
                                  color: w.connected ? "var(--ok)" : "var(--muted)" } }, icon(w.enabled ? "wifi" : "wifi-off")),
          h("div.grow", null,
            h("div", { style: { fontWeight: 600, fontSize: "16px" } },
              hs ? "Hotspot " + hs.ssid : w.connected ? w.ssid : w.enabled ? "Not connected" : "Wi-Fi is off"),
            h("div.muted", { style: { fontSize: "13px" } },
              [w.device, hs ? "not on a network while the hotspot is on" : w.connected ? w.ip : w.state,
               w.connected && w.signal ? w.signal + "%" : null].filter(Boolean).join(" · "))),
          w.connected ? h("button.btn.sm", { onclick: (e) => disconnect(e.currentTarget) }, "Disconnect") : null,
          radio));
    }

    // ------------------------------------------------------------- hotspot (WIFI-08)
    async function loadHotspot() {
      try { hotspot = await conn.call("wifi.hotspot"); } catch (e) { hotspot = { error: errText(e) }; }
      renderHotspot();
    }

    function renderHotspot() {
      const hs = hotspot;
      if (!hs) { clear(hotspotCard, h("div.empty", null, h("span.spin", { style: { margin: "0 auto" } }))); return; }
      if (hs.error || hs.supported === false) {
        clear(hotspotCard, h("div.muted", { style: { fontSize: "13px" } }, hs.error ||
          (hs.device ? hs.device + " cannot run a hotspot (no access point mode)." : "No Wi-Fi device.")));
        return;
      }
      const live = (store.get("wifi") || {}).hotspot;
      const on = !!(live && live.active);
      const meta = on
        ? [hs.band, live.ip, (live.clients ?? 0) + (live.clients === 1 ? " device" : " devices") + " joined"]
        : [hs.band, "the way in without a router: join it, open the address shown here"];
      const pw = hs.password
        ? h("span.mono", null, showPw ? hs.password : "•".repeat(Math.min(hs.password.length, 12)))
        : h("span.muted", null, "made when it first starts");
      clear(hotspotCard,
        h("div.row", null,
          h("div.ico", { style: { width: "44px", height: "44px", borderRadius: "var(--r)", display: "grid", placeItems: "center",
                                  background: on ? "var(--ok-soft)" : "var(--card-3)", color: on ? "var(--ok)" : "var(--muted)" } },
            icon("hotspot")),
          h("div.grow", { style: { minWidth: 0 } },
            h("div.ellipsis", { style: { fontWeight: 600, fontSize: "16px" } }, hs.ssid),
            h("div.muted", { style: { fontSize: "13px" } }, (on ? "On · " : "Off · ") + meta.filter(Boolean).join(" · "))),
          on ? null : h("button.btn.sm.ghost", { onclick: () => editHotspot() }, "Settings"),
          on ? h("button.btn.sm", { onclick: (e) => stopHotspot(e.currentTarget) }, "Stop")
             : h("button.btn.sm.primary", { onclick: (e) => startHotspot(e.currentTarget) }, "Start")),
        h("dl.kv", { style: { marginTop: "14px" } },
          h("dt", null, "Password"),
          h("dd", null, pw, hs.password ? h("span", { style: { marginLeft: "10px", whiteSpace: "nowrap" } },
            h("button.btn.sm.ghost", { onclick: () => { showPw = !showPw; renderHotspot(); } }, showPw ? "Hide" : "Show"),
            h("button.btn.sm.ghost", { onclick: () => copyText(hs.password) }, "Copy")) : null),
          on && live.web ? [h("dt", null, "Web address"),
            h("dd", null, h("span.mono", null, live.web),
              h("button.btn.sm.ghost", { style: { marginLeft: "10px" }, onclick: () => copyText(live.web) }, "Copy"))] : null));
    }

    function editHotspot() {
      const name = h("input.input", { value: hotspot.ssid || "", maxlength: 32, spellcheck: "false", autocomplete: "off" });
      const pass = h("input.input", { type: "text", value: "", placeholder: hotspot.has_password ? "unchanged" : "a random one",
                                      spellcheck: "false", autocomplete: "new-password", class: "mono" });
      let band = hotspot.band === "5 GHz" ? "5" : "2.4";
      const s = sheet({
        title: "Hotspot settings",
        body: h("div.stack", null,
          h("div.field", null, h("label", null, "Name"), name),
          h("div.field", null, h("label", null, "Password"), pass, h("div.hint", null, "8 to 63 characters; empty keeps the current one")),
          h("div.field", null, h("label", null, "Band"),
            seg([["2.4", "2.4 GHz"], ["5", "5 GHz"]], band, (v) => { band = v; }),
            h("div.hint", null, "2.4 GHz reaches further and every device knows it; 5 GHz is faster."))),
        foot: [h("button.btn.ghost", { onclick: () => s.close() }, "Cancel"),
               h("button.btn.primary", { onclick: (e) => save(e.currentTarget) }, "Save and start")],
      });
      async function save(btn) {
        const args = { ssid: name.value.trim(), password: pass.value || null, band };
        if (!(await confirmLeave(args.ssid))) return;
        s.close();
        await start(args, btn);
      }
    }

    async function confirmLeave(ssid) {
      const w = store.get("wifi") || {};
      if (!w.connected || (w.hotspot && w.hotspot.active)) return true;
      return confirmBox({ title: "Start the hotspot?", ok: "Start",
        text: "The Pi leaves " + w.ssid + " while its hotspot is on (one radio): anything that reaches it over " + w.ssid +
              " - this page too, if it does - loses it. Join " + (ssid || hotspot.ssid) +
              " and open the web address shown here. Bluetooth keeps working; stopping the hotspot brings " + w.ssid + " back." });
    }

    async function startHotspot(btn) {
      if (await confirmLeave()) await start({}, btn);
    }

    async function start(args, btn) {
      toast("Starting the hotspot…");
      const r = await run("wifi.hotspot.start", args, { btn });
      if (r) { hotspot = r.hotspot; renderHotspot(); toast(r.message, "ok"); }
    }

    async function stopHotspot(btn) {
      const r = await run("wifi.hotspot.stop", {}, { btn });
      if (r) { toast(r.message, "ok"); loadHotspot(); }
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
        text: "If you reach the Pi over this Wi-Fi, this page loses it. The app's Bluetooth link keeps working." });
      if (ok) await run("wifi.disconnect", {}, { ok: "Disconnected", btn });
    }

    async function forget(n) {
      const ok = await confirmBox({ title: "Forget " + (n.ssid || n.name) + "?", ok: "Forget", danger: true,
                                    text: "The saved password is removed from the Pi." });
      if (ok && await run("wifi.forget", { uuid: n.uuid }, { ok: "Forgotten" })) scan(false);
    }

    let wasOn = null;
    const off = store.on("wifi", (w) => {
      renderStatus(w);
      const on = !!(w && w.hotspot && w.hotspot.active);
      if (wasOn !== null && on !== wasOn) loadHotspot(); else renderHotspot();
      wasOn = on;
    });
    if (!store.get("wifi")) renderStatus({});
    renderHotspot();
    renderNetworks();
    renderSaved();
    const ready = () => { scan(false); loadHotspot(); };
    const offReady = conn.on("ready", ready);
    if (conn.status === "open") ready();
    return () => { off(); offReady(); };
  },
};

