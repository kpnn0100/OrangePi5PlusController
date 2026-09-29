// Bluetooth devices (NET-04/05): the Pi's adapter, scan, pair / connect keyboards, mice,
// headsets... (BlueZ). The phone link of the app is under System › Bluetooth pairing.

import { h, clear, icon, conn, run, toggle, confirmBox, toast, errText, busy } from "../core.js";

const DEV_ICON = { "input-keyboard": "keyboard", "input-mouse": "remote", phone: "phone", "audio-card": "volume",
                   "audio-headset": "volume", "audio-headphones": "volume", computer: "screen" };

export default {
  id: "bluetooth", title: "Bluetooth", icon: "bluetooth",

  mount(root) {
    let status = null;
    let devices = null;
    let scanning = false;
    const head = h("div.card");
    const list = h("div.card", { style: { padding: "6px 14px" } });
    const scanBtn = h("button.btn.sm", { onclick: () => scan() }, icon("refresh"), "Scan");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Bluetooth"), h("div.sub", null, "Devices the Pi uses: keyboards, mice, speakers..."))),
      head,
      h("div.section", null, h("div.section-title", null, "Devices", h("div.actions", null, scanBtn)), list));

    function renderHead() {
      const s = status || {};
      if (!status) return clear(head, h("div.row", null, h("span.spin"), h("span.muted", null, "Loading…")));
      if (!s.present) return clear(head, h("div.muted", null, "No Bluetooth adapter: " + (s.error || "")));
      clear(head, h("div.row", null,
        h("div.ico", { style: { width: "44px", height: "44px", borderRadius: "13px", display: "grid", placeItems: "center",
                                background: s.powered ? "var(--accent-soft)" : "var(--card-3)", color: s.powered ? "var(--accent)" : "var(--muted)" } },
          icon("bluetooth")),
        h("div.grow", null, h("div", { style: { fontWeight: 600 } }, s.alias || s.name || "Adapter"),
          h("div.muted.mono", { style: { fontSize: "12.5px" } }, [s.address, s.discovering ? "scanning" : null].filter(Boolean).join(" · "))),
        toggle(s.powered, async (on) => { const r = await run("bt.power", { on }, { ok: on ? "Bluetooth on" : "Bluetooth off" }); if (r) { status = r; renderHead(); } },
               { label: "Bluetooth power" })));
    }

    function renderList() {
      if (!devices) return clear(list, h("div.empty", null, h("span.spin", { style: { margin: "0 auto" } })));
      if (!devices.length) return clear(list, h("div.empty", null, "No devices yet - put one in pairing mode and scan."));
      clear(list, h("div.list", null, devices.map((d) => h("div.list-item", null,
        h("div.ico", { style: { color: d.connected ? "var(--ok)" : "" } }, icon(DEV_ICON[d.icon] || "bluetooth")),
        h("div.grow", null, h("div.title.ellipsis", null, d.alias || d.name || d.address),
          h("div.meta.mono", null, [d.address, d.paired ? "paired" : null, d.trusted ? "trusted" : null,
                                    d.rssi !== undefined && d.rssi !== null ? d.rssi + " dBm" : null].filter(Boolean).join(" · "))),
        d.connected ? h("span.tag.ok", null, "Connected") : null,
        !d.paired ? h("button.btn.sm.primary", { onclick: (e) => act("pair", d, e.currentTarget) }, "Pair")
          : d.connected ? h("button.btn.sm.ghost", { onclick: (e) => act("disconnect", d, e.currentTarget) }, "Disconnect")
          : h("button.btn.sm", { onclick: (e) => act("connect", d, e.currentTarget) }, "Connect"),
        d.paired ? h("button.btn.sm.ghost.danger", { onclick: () => forget(d) }, "Forget") : null))));
    }

    async function load() {
      try {
        const [s, r] = await Promise.all([conn.call("bt.status"), conn.call("bt.devices")]);
        status = s;
        devices = r.devices;
      } catch (e) {
        toast(errText(e), "err");
        devices = devices || [];
      }
      renderHead();
      renderList();
    }

    async function scan() {
      if (scanning) return;
      scanning = true;
      await busy(scanBtn, async () => { devices = (await conn.call("bt.scan", { seconds: 8 }, 60000)).devices; });
      scanning = false;
      renderList();
    }

    async function act(what, d, btn) {
      const r = await run("bt." + what, { address: d.address }, { btn, ok: { pair: "Paired", connect: "Connected", disconnect: "Disconnected" }[what] });
      if (r && what === "pair") await conn.call("bt.trust", { address: d.address }).catch(() => {});
      if (r) load();
    }

    async function forget(d) {
      const ok = await confirmBox({ title: `Forget ${d.alias || d.name || d.address}?`, ok: "Forget", danger: true,
                                    text: "It has to be paired again to be used." });
      if (ok && await run("bt.remove", { address: d.address }, { ok: "Forgotten" })) load();
    }

    renderHead();
    renderList();
    const offReady = conn.on("ready", load);
    if (conn.status === "open") load();
    return () => offReady();
  },
};
