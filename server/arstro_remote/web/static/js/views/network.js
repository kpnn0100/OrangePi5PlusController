// Network (NET-01..03): network devices and NetworkManager profiles - Ethernet addressing
// (DHCP / static), any saved profile: edit, activate, deactivate, delete.

import { h, clear, icon, store, conn, run, sheet, seg, toggle, confirmBox, toast, errText, busy } from "../core.js";

const TYPE_ICON = { ethernet: "ethernet", wifi: "wifi", "802-11-wireless": "wifi", "802-3-ethernet": "ethernet",
                    bt: "bluetooth", bluetooth: "bluetooth" };
const TYPE_NAME = { "802-3-ethernet": "Ethernet", "802-11-wireless": "Wi-Fi", bluetooth: "Bluetooth", bridge: "Bridge",
                    vpn: "VPN", wireguard: "WireGuard", "wifi-p2p": "Wi-Fi P2P" };

export default {
  id: "network", title: "Network", icon: "ethernet",

  mount(root) {
    const devCard = h("div.card", { style: { padding: "6px 14px" } });
    const conList = h("div.card", { style: { padding: "6px 14px" } });
    const addBtn = h("button.btn.sm", { onclick: () => editor(null) }, icon("plus"), "Ethernet profile");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Network"), h("div.sub", null, "Interfaces and saved connections (NetworkManager)")),
        h("div.actions", null, h("button.btn.sm.ghost", { onclick: (e) => refresh(e.currentTarget) }, icon("refresh"), "Refresh"))),
      h("div.section-title", null, "Interfaces"), devCard,
      h("div.section", null, h("div.section-title", null, "Connections", h("div.actions", null, addBtn)), conList));

    function renderDevices(net) {
      const devs = (net && net.devices) || null;
      if (!devs) return clear(devCard, h("div.empty", null, net && net.error ? net.error : h("span.spin", { style: { margin: "0 auto" } })));
      clear(devCard, h("div.list", null, devs.filter((d) => d.type !== "wifi-p2p").map((d) => {
        const up = d.state.startsWith("connected");
        return h("div.list-item", null,
          h("div.ico", { style: { color: up ? "var(--ok)" : "var(--muted)" } }, icon(TYPE_ICON[d.type] || "net")),
          h("div.grow", null,
            h("div.title", null, h("span.mono", null, d.device), " ", h("span.muted", null, TYPE_NAME[d.type] || d.type)),
            h("div.meta.ellipsis", null, [d.state, d.connection, (d.ip4 || []).join(", "), d.gateway ? "gw " + d.gateway : null,
                                          d.carrier === false ? "no cable" : null, d.hwaddr].filter(Boolean).join(" · "))),
          d.type === "ethernet" ? (up
            ? h("button.btn.sm.ghost", { onclick: (e) => devAction(d, false, e.currentTarget) }, "Disconnect")
            : h("button.btn.sm", { disabled: d.state === "unavailable", onclick: (e) => devAction(d, true, e.currentTarget) }, "Connect")) : null);
      })));
    }

    function renderConnections(net) {
      const cons = (net && net.connections) || null;
      if (!cons) return clear(conList, h("div.empty", null, h("span.spin", { style: { margin: "0 auto" } })));
      if (!cons.length) return clear(conList, h("div.empty", null, "No saved connections."));
      clear(conList, h("div.list", null, cons.map((c) => h("div.list-item.click", { onclick: () => editor(c) },
        h("div.ico", null, icon(TYPE_ICON[c.type] || "net")),
        h("div.grow", null, h("div.title.ellipsis", null, c.name),
          h("div.meta", null, [TYPE_NAME[c.type] || c.type, c.device, c.autoconnect ? "auto-connect" : "manual"].filter(Boolean).join(" · "))),
        c.active ? h("span.tag.ok", null, "Active") : null, icon("chevron", "dim")))));
    }

    async function refresh(btn) {
      await busy(btn, async () => { await conn.call("net.status"); });
    }

    async function devAction(d, on, btn) {
      if (!on) {
        const ok = await confirmBox({ title: `Disconnect ${d.device}?`, ok: "Disconnect", danger: true,
          text: "If you reach the Pi over this interface you will lose this page." });
        if (!ok) return;
      }
      await run(on ? "net.device.connect" : "net.device.disconnect", { device: d.device }, { btn, ok: on ? "Connected" : "Disconnected" });
    }

    async function editor(c) {
      let prof = null;
      if (c) {
        try { prof = await conn.call("net.connection.get", { uuid: c.uuid }); } catch (e) { return toast(errText(e), "err"); }
      }
      const devs = ((store.get("net") || {}).devices || []).filter((d) => d.type === "ethernet").map((d) => d.device);
      const v4 = (prof && prof.ipv4) || { method: "auto", addresses: [], dns: [] };
      let method = v4.method || "auto";
      const name = h("input.input", { value: prof ? prof.name : "Static " + (devs[0] || "eth"), spellcheck: "false" });
      const iface = prof ? null : h("select.input", null, devs.map((d) => h("option", { value: d }, d)));
      const addr = h("input.input", { value: (v4.addresses || []).join(", "), placeholder: "192.0.2.50/24", spellcheck: "false" });
      const gw = h("input.input", { value: v4.gateway || "", placeholder: "192.0.2.1", spellcheck: "false" });
      const dns = h("input.input", { value: (v4.dns || []).join(", "), placeholder: "1.1.1.1, 8.8.8.8", spellcheck: "false" });
      const mtu = h("input.input", { value: prof && prof.mtu && prof.mtu !== "auto" ? prof.mtu : "", placeholder: "auto", inputmode: "numeric" });
      let auto = prof ? prof.autoconnect : true;
      const staticBox = h("div.stack");
      const renderStatic = () => clear(staticBox, method === "manual" || method === "shared" ? [
        h("div.field", null, h("label", null, "Addresses"), addr, h("div.hint", null, "One or more, comma separated, with prefix length")),
        method === "manual" ? h("div.field", null, h("label", null, "Gateway"), gw) : null] : null,
        method !== "disabled" ? h("div.field", null, h("label", null, "DNS servers"), dns,
          h("div.hint", null, method === "auto" ? "Extra servers besides the ones DHCP gives" : "")) : null);
      renderStatic();
      const isEth = !prof || prof.type === "802-3-ethernet";
      const body = h("div.stack", null,
        prof ? h("div.muted", { style: { fontSize: "13px" } }, `${TYPE_NAME[prof.type] || prof.type}${prof.interface ? " · " + prof.interface : ""} · ${prof.uuid}`) : null,
        h("div.field", null, h("label", null, "Name"), name),
        iface ? h("div.field", null, h("label", null, "Interface"), iface) : null,
        h("div.field", null, h("label", null, "IPv4"),
          seg([["auto", "DHCP"], ["manual", "Static"], ["shared", "Share"], ["disabled", "Off"]], method,
              (v) => { method = v; renderStatic(); }, { full: true })),
        staticBox,
        isEth ? h("div.field", null, h("label", null, "MTU"), mtu) : null,
        h("div.row", null, h("div.grow", null, h("b", null, "Connect automatically")),
          toggle(auto, (v) => { auto = v; })));
      const params = () => {
        const p = { name: name.value.trim(), ipv4_method: method, autoconnect: auto };
        if (method === "manual" || method === "shared") p.addresses = addr.value;
        if (method === "manual") p.gateway = gw.value.trim();
        if (method !== "disabled") p.dns = dns.value;
        if (isEth && mtu.value.trim()) p.mtu = mtu.value.trim();
        return p;
      };
      const s = sheet({
        title: prof ? prof.name : "New Ethernet profile", body,
        foot: [
          prof ? h("button.btn.ghost.danger", { onclick: async () => {
            const ok = await confirmBox({ title: `Delete ${prof.name}?`, ok: "Delete", danger: true, text: "The profile is removed from the Pi." });
            if (ok && await run("net.connection.delete", { uuid: prof.uuid }, { ok: "Deleted" })) s.close();
          } }, "Delete") : null,
          prof ? h("button.btn", { onclick: (e) => run(c.active ? "net.connection.down" : "net.connection.up", { uuid: prof.uuid },
                                                            { btn: e.currentTarget, ok: c.active ? "Deactivated" : "Activated" }).then((r) => r && s.close()) },
                   c.active ? "Deactivate" : "Activate") : null,
          h("button.btn.primary", { onclick: async (e) => {
            const r = prof ? await run("net.connection.set", { uuid: prof.uuid, ...params() }, { btn: e.currentTarget, ok: "Saved - reactivate to apply" })
                           : await run("net.connection.add_ethernet", { interface: iface.value, ...params() }, { btn: e.currentTarget, ok: "Profile added" });
            if (r) s.close();
          } }, prof ? "Save" : "Add"),
        ],
      });
    }

    const off = store.on("net", (n) => { renderDevices(n); renderConnections(n); });
    if (!store.get("net")) { renderDevices(null); renderConnections(null); }
    const start = () => conn.call("net.status").catch((e) => toast(errText(e), "err"));
    const offReady = conn.on("ready", start);
    if (conn.status === "open") start();
    return () => { off(); offReady(); };
  },
};
