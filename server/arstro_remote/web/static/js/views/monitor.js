// Monitor (STAT-01/02): live system stats while the view is open.

import { h, clear, icon, conn, fmtBytes, fmtUptime, signalBars } from "../core.js";

export default {
  id: "monitor", title: "Monitor", icon: "monitor",

  mount(root) {
    const sub = h("div.sub");
    const body = h("div.grid.three");
    const lower = h("div.grid.two", { style: { marginTop: "16px" } });
    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Monitor"), sub)), body, lower);
    clear(body, h("div.card", null, h("div.row", null, h("span.spin"), h("span.muted", null, "Loading…"))));

    const tone = (p) => (p >= 90 ? "err" : p >= 70 ? "warn" : "");
    const bar = (p) => h("div.bar", { class: tone(p) }, h("i", { style: { width: Math.min(100, p || 0) + "%" } }));

    function render(s) {
      sub.textContent = `${s.hostname} · ${s.os} · up ${fmtUptime(s.uptime)}`;
      const cpu = s.cpu || {}, mem = s.memory || {}, sw = s.swap || {}, temps = s.temps || {}, dev = s.devfreq || {};
      const maxCore = Math.max(1, ...(cpu.per_core || [1]));
      const freq = (cpu.freq_mhz || []).map((f) => (f / 1000).toFixed(1)).join(" / ");
      const hottest = Object.entries(temps).sort((a, b) => b[1] - a[1]);

      clear(body,
        h("div.card", null,
          h("div.card-title", null, icon("cpu"), "CPU"),
          h("div.big", null, Math.round(cpu.percent || 0), h("small", null, "%")),
          h("div.cores", { style: { margin: "14px 0 10px" }, title: "Per core" },
            (cpu.per_core || []).map((p) => h("i", { style: { height: Math.max(4, (p / Math.max(100, maxCore)) * 100) + "%" }, title: p + "%" }))),
          h("div.muted", { style: { fontSize: "12.5px" } }, `${cpu.count || "?"} cores · ${freq} GHz · load ${(cpu.load || []).map((x) => x.toFixed(2)).join(" ")}`)),
        h("div.card", null,
          h("div.card-title", null, icon("memory"), "Memory"),
          h("div.big", null, fmtBytes(mem.used), h("small", null, "of " + fmtBytes(mem.total))),
          h("div", { style: { margin: "14px 0 10px" } }, bar(mem.percent)),
          h("div.muted", { style: { fontSize: "12.5px" } }, `${mem.percent}% used · swap ${fmtBytes(sw.used)} of ${fmtBytes(sw.total)}`)),
        h("div.card", null,
          h("div.card-title", null, icon("thermo"), "Temperature"),
          h("div.big", null, Math.round(s.cpu_temp || (hottest[0] || [0, 0])[1]), h("small", null, "°C")),
          h("div.kv", { style: { marginTop: "12px", fontSize: "13px" } },
            hottest.slice(0, 6).flatMap(([k, v]) => [h("dt", null, k), h("dd.num", null, v.toFixed(1) + " °C")]))),
        h("div.card", null,
          h("div.card-title", null, icon("monitor"), "Accelerators"),
          h("div.stack", null,
            ["gpu", "npu", "dmc"].filter((k) => dev[k]).map((k) => h("div", null,
              h("div.row.between", { style: { fontSize: "13px", marginBottom: "6px" } },
                h("span", null, { gpu: "GPU", npu: "NPU", dmc: "Memory bus" }[k]),
                h("span.muted.num", null, `${dev[k].load}% · ${dev[k].mhz} MHz`)),
              bar(dev[k].load))),
            s.fan_percent !== undefined && s.fan_percent !== null ? h("div.muted", { style: { fontSize: "12.5px" } }, `Fan ${s.fan_percent}%`) : null)),
        h("div.card", null,
          h("div.card-title", null, icon("disk"), "Storage"),
          h("div.stack", null, (s.disks || []).map((d) => h("div", null,
            h("div.row.between", { style: { fontSize: "13px", marginBottom: "6px" } },
              h("span.mono.ellipsis", null, d.mount), h("span.muted.num", null, `${fmtBytes(d.free)} free`)),
            bar(d.percent))))),
        h("div.card", null,
          h("div.card-title", null, icon("net"), "Network"),
          h("dl.kv", { style: { margin: 0 } },
            h("dt", null, "IP"), h("dd.mono", null, s.network?.ip || "–"),
            h("dt", null, "Gateway"), h("dd.mono", null, s.network?.gateway || "–"),
            h("dt", null, "Wi-Fi"), h("dd", null, s.wifi?.ssid ? h("span.row", { style: { gap: "8px" } }, s.wifi.ssid, signalBars(s.wifi.signal)) : "not connected"),
            ...(s.network?.interfaces || []).filter((i) => i.up && (i.rx_rate || i.tx_rate || (i.ipv4 || []).length)).flatMap((i) => [
              h("dt.ellipsis", null, i.name),
              h("dd.num", null, `↓ ${fmtBytes(i.rx_rate)}/s  ↑ ${fmtBytes(i.tx_rate)}/s`)]))));

      clear(lower,
        h("div.card", null,
          h("div.card-title", null, "Top processes"),
          h("table.proc", null,
            h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "CPU"), h("th", null, "Mem"), h("th", null, "User"))),
            h("tbody", null, (s.processes || []).map((p) => h("tr", null,
              h("td", { title: p.name }, p.name), h("td", null, p.cpu.toFixed(1) + "%"), h("td", null, p.mem.toFixed(1) + "%"),
              h("td.muted", null, p.user)))))),
        h("div.card", null,
          h("div.card-title", null, "System"),
          h("dl.kv", { style: { margin: 0 } },
            h("dt", null, "Host"), h("dd", null, s.hostname),
            h("dt", null, "OS"), h("dd", null, s.os),
            h("dt", null, "Kernel"), h("dd.mono", null, s.kernel),
            h("dt", null, "Arch"), h("dd", null, s.arch),
            h("dt", null, "Uptime"), h("dd", null, fmtUptime(s.uptime)))));
    }

    const off = conn.on("event", (ev, msg) => { if (ev === "stats") render(msg.data); });
    const start = () => conn.call("stats.get").then(render).catch(() => {}).then(() =>
      conn.call("stats.subscribe", { interval_ms: 2000 }).catch(() => {}));
    const offReady = conn.on("ready", start);
    if (conn.status === "open") start();
    return () => {
      off();
      offReady();
      conn.call("stats.unsubscribe").catch(() => {});
    };
  },
};
