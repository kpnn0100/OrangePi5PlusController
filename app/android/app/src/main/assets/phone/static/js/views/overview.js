// Overview (PHN-03): battery, storage, memory, network, signal, screen - live from the `status`
// topic - and quick actions: ring, locate, torch, lock the screen.

import { h, clear, icon, store, run, op, fmtBytes, fmtUptime, signalBars, featureReady, featureReason, batteryPct,
         toast, errText, busy } from "../core.js";

export default {
  mount(root, { go }) {
    const info = store.get("info") || {};
    const head = h("div.view-head", null,
      h("div", null, h("h1", null, "Overview"),
        h("div.sub", null, [info.manufacturer, info.model, info.android ? "Android " + info.android : null,
                            info.app_version ? "app " + info.app_version : null].filter(Boolean).join(" · "))),
      h("div.actions", null, h("button.btn.sm", { onclick: (e) => refresh(e.currentTarget) }, icon("refresh"), "Refresh")));

    // ------------------------------------------------------ quick actions
    const ringBtn = h("button.btn", { onclick: (e) => ring(e.currentTarget) });
    const locateBtn = h("button.btn", { onclick: () => go("find") }, icon("pin"), "Locate");
    const torchBtn = h("button.btn", { onclick: (e) => torch(e.currentTarget) });
    const lockBtn = h("button.btn", { onclick: (e) => run("screen.key", { key: "lock" }, { btn: e.currentTarget, ok: "Screen locked" }) },
                      icon("lock"), "Lock screen");
    const quick = h("div.card", null, h("div.card-title", null, "Quick actions"),
      h("div.quick", null, ringBtn, locateBtn, torchBtn, lockBtn));
    const gate = (btn, featureId) => {
      const ok = featureReady(featureId);
      btn.disabled = !ok;
      btn.title = ok ? "" : featureReason(featureId);
    };
    function renderQuick() {
      const ringing = !!(store.get("find") || {}).ringing;
      clear(ringBtn, icon(ringing ? "stop" : "ring"), ringing ? "Stop ringing" : "Ring");
      ringBtn.classList.toggle("red", ringing);
      const torchOn = !!store.get("torch.local");      // the phone has no torch topic: what this browser last set
      clear(torchBtn, icon("torch"), torchOn ? "Torch off" : "Torch");
      torchBtn.classList.toggle("on", torchOn);
      gate(ringBtn, "find"); gate(locateBtn, "find"); gate(torchBtn, "camera"); gate(lockBtn, "screen");
    }
    async function ring(btn) {
      const ringing = !!(store.get("find") || {}).ringing;
      const r = await run(ringing ? "find.ring_stop" : "find.ring", ringing ? {} : { seconds: 60 },
                          { btn, ok: ringing ? "Stopped ringing" : "The phone is ringing" });
      if (r) store.set("find", { ...(store.get("find") || {}), ringing: !ringing });
    }
    async function torch(btn) {
      const on = !store.get("torch.local");
      const r = await run("camera.torch", { on }, { btn });
      if (r) store.set("torch.local", on);
    }

    // ------------------------------------------------------------- status
    const cards = h("div.ov-grid");
    const statusBox = h("div.section");
    function bar(used, total) {
      const p = total ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
      return h("div.bar", { class: p > 90 ? "err" : p > 75 ? "warn" : "" }, h("i", { style: { width: p.toFixed(1) + "%" } }));
    }
    function renderStatus(st) {
      if (!featureReady("status")) {
        clear(statusBox, h("div.note-box.warn", null, icon("info"), h("span", null, featureReason("status"))));
        return;
      }
      if (!st) { clear(statusBox, h("div.empty.loading", null, h("span.spin"), h("div", null, "Waiting for the phone's status…"))); return; }
      const b = st.battery || {}, s = st.storage || {}, m = st.memory || {}, n = st.network || {}, sig = st.signal;
      const pct = batteryPct(b.level);
      clear(cards,
        h("div.card", { "data-card": "battery" }, h("div.card-title", null, icon("battery"), "Battery"),
          h("div.big-num", null, isNaN(pct) ? "–" : pct, h("small", null, "%")),
          h("div.bar", { class: pct <= 15 ? "err" : b.charging ? "ok" : "" }, h("i", { style: { width: (pct || 0) + "%" } })),
          h("dl.kv", { style: { marginTop: "12px" } },
            h("dt", null, "State"), h("dd", null, b.charging ? "Charging" + (b.plugged ? ` (${b.plugged})` : "") : b.plugged ? `Plugged (${b.plugged})` : "On battery"),
            b.temperature_c !== undefined && b.temperature_c !== null ? [h("dt", null, "Temperature"), h("dd", null, Number(b.temperature_c).toFixed(1) + " °C")] : null,
            b.health ? [h("dt", null, "Health"), h("dd", null, String(b.health))] : null)),
        h("div.card", { "data-card": "storage" }, h("div.card-title", null, icon("disk"), "Storage"),
          h("div.big-num", null, fmtBytes(s.free), h("small", null, "free")),
          bar((s.total || 0) - (s.free || 0), s.total),
          h("div.hint", { style: { marginTop: "8px" } }, `${fmtBytes((s.total || 0) - (s.free || 0))} used of ${fmtBytes(s.total)}`),
          h("div.card-title", { style: { marginTop: "16px" } }, icon("memory"), "Memory"),
          bar((m.total || 0) - (m.available || 0), m.total),
          h("div.hint", { style: { marginTop: "8px" } }, `${fmtBytes(m.available)} available of ${fmtBytes(m.total)}`)),
        h("div.card", { "data-card": "network" }, h("div.card-title", null, icon("wifi"), "Network"),
          h("div.big-num", { style: { fontSize: "24px" } }, n.connected === false ? "Offline" : String(n.type || "–")),
          h("dl.kv", { style: { marginTop: "10px" } },
            n.wifi_ssid ? [h("dt", null, "Wi-Fi"), h("dd", null, n.wifi_ssid)] : null,
            n.ip ? [h("dt", null, "IP"), h("dd.mono", null, n.ip)] : null,
            sig ? [h("dt", null, "Signal"), h("dd", null, signalBars(Number(sig.level) || 0), " ", sig.type || "")] : null)),
        h("div.card", { "data-card": "screen" }, h("div.card-title", null, icon("phone"), "Phone"),
          h("dl.kv", null,
            h("dt", null, "Screen"), h("dd", null, st.screen_on ? "On" : "Off", st.locked ? " · locked" : st.locked === false ? " · unlocked" : ""),
            st.uptime_s !== undefined ? [h("dt", null, "Up for"), h("dd", null, fmtUptime(st.uptime_s))] : null,
            info.android ? [h("dt", null, "Android"), h("dd", null, `${info.android}` + (info.sdk ? ` (SDK ${info.sdk})` : ""))] : null,
            info.app_version ? [h("dt", null, "App"), h("dd", null, "Arstro Remote " + info.app_version)] : null)));
      clear(statusBox, cards);
    }

    async function refresh(btn) {
      if (!featureReady("status")) return;
      await busy(btn, async () => { store.set("status", await op("status.get")); });
    }

    root.append(head, quick, statusBox);
    const offs = [
      store.on("status", renderStatus),
      store.on("find", renderQuick),
      store.on("torch.local", renderQuick, { now: false }),
      store.on("features", () => { renderQuick(); renderStatus(store.get("status")); }),
    ];
    renderQuick();
    renderStatus(store.get("status"));
    // the topic comes with /ws; if it has not come soon, ask once
    const late = setTimeout(() => {
      if (!store.get("status") && featureReady("status")) op("status.get").then((st) => store.set("status", st)).catch((e) => toast(errText(e), "err"));
    }, 2000);
    return () => { clearTimeout(late); offs.forEach((f) => f()); };
  },
};
