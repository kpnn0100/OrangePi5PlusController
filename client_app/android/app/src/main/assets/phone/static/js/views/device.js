// Device controls (PHN-24): ringer mode and Do Not Disturb, Bluetooth (Android 12), an alarm or a timer
// in the clock app, the screen timeout, the wallpaper from a file on the phone, the sensors, and facts.

import { h, clear, icon, op, run, seg, toast, errText, fmtUptime, spinnerBlock } from "../core.js";

export default {
  mount(root) {
    const ringer = seg([["normal", "Sound"], ["vibrate", "Vibrate"], ["silent", "Silent"]], null,
      (mode) => run("device.ringer", { mode }, { ok: "Ringer: " + mode }).then((r) => r && ringer.set(r.mode)), { label: "Ringer" });
    const dnd = seg([["off", "Off"], ["priority", "Priority"], ["alarms", "Alarms"], ["none", "Silence"]], null,
      (mode) => run("device.dnd", { mode }, { ok: "Do Not Disturb: " + mode }).then((r) => r && dnd.set(r.mode)), { label: "Do Not Disturb", full: true });
    const btState = h("small", null, "…");
    const btOn = h("button.btn.sm", { onclick: (e) => bt(true, e.currentTarget) }, "Turn on");
    const btOff = h("button.btn.sm", { onclick: (e) => bt(false, e.currentTarget) }, "Turn off");
    async function bt(on, btn) {
      const r = await run("device.bluetooth", { on }, { btn, ok: "Bluetooth " + (on ? "on" : "off") });
      if (r) btState.textContent = r.on ? "On" + (r.name ? " · " + r.name : "") : "Off";
    }

    const now = new Date();
    const hourIn = h("input.input", { type: "number", min: "0", max: "23", value: String(now.getHours()), "aria-label": "Hour", style: { width: "76px" } });
    const minIn = h("input.input", { type: "number", min: "0", max: "59", value: String((now.getMinutes() + 5) % 60), "aria-label": "Minute", style: { width: "76px" } });
    const labelIn = h("input.input", { type: "text", placeholder: "Label (optional)", maxlength: "60", "aria-label": "Alarm label" });
    const timerIn = h("input.input", { type: "number", min: "1", max: "1440", value: "5", "aria-label": "Timer minutes", style: { width: "90px" } });

    const timeoutSel = h("select.input", { "aria-label": "Screen timeout", style: { width: "auto" },
      onchange: () => run("device.screen_timeout", { seconds: Number(timeoutSel.value) }, { ok: "Screen timeout changed" }) },
      [15, 30, 60, 120, 300, 600, 1800].map((s) => h("option", { value: String(s) }, s < 60 ? s + " s" : s / 60 + " min")));

    const wallIn = h("input.input", { type: "text", placeholder: "/DCIM/Camera/photo.jpg", "aria-label": "Picture on the phone" });
    let which = "both";
    const wallWhich = seg([["both", "Both"], ["home", "Home"], ["lock", "Lock"]], which, (v) => { which = v; }, { label: "Screen" });

    const facts = h("dl.kv", null, spinnerBlock());
    const sensors = h("div.stack", null, spinnerBlock("Loading sensors…"));

    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Device"), h("div.sub", null, "Sound modes, Bluetooth, clock, settings and sensors"))),
      h("div.grid.two", null,
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("ring"), "Sound"),
          h("div.set-row", null, h("div.txt", null, h("b", null, "Ringer"), h("small", null, "Silent and back need Do Not Disturb access")), ringer),
          h("div", null, h("div.lbl", { style: { margin: "4px 0 8px" } }, "Do Not Disturb"), dnd),
          h("div.set-row", null, h("div.txt", null, h("b", null, "Bluetooth"), btState), h("div.row", null, btOn, btOff))),
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("clock"), "Clock"),
          h("div.row.wrap", null, hourIn, h("span", null, ":"), minIn, labelIn,
            h("button.btn.primary", { onclick: (e) => run("device.alarm", { hour: Number(hourIn.value), minute: Number(minIn.value), label: labelIn.value.trim() || undefined },
              { btn: e.currentTarget, ok: "Alarm set on the phone" }) }, icon("clock"), "Set alarm")),
          h("div.row.wrap", null, timerIn, h("span.muted", null, "minutes"),
            h("button.btn", { onclick: (e) => run("device.timer", { seconds: Math.round(Number(timerIn.value) * 60), label: labelIn.value.trim() || undefined },
              { btn: e.currentTarget, ok: "Timer started on the phone" }) }, "Start timer")),
          h("div.hint", null, "Set in the phone's clock app (it needs \"Display over other apps\" to start from the background).")),
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("sliders"), "Settings"),
          h("div.set-row", null, h("div.txt", null, h("b", null, "Screen timeout"), h("small", null, "Needs \"Modify system settings\"")), timeoutSel),
          h("div", null, h("div.lbl", { style: { margin: "4px 0 8px" } }, "Wallpaper from a picture on the phone"),
            h("div.row.wrap", null, wallIn, wallWhich,
              h("button.btn", { onclick: (e) => {
                const path = wallIn.value.trim();
                if (!path) { toast("Give the path of a picture on the phone (see Files)", "err"); return; }
                run("device.wallpaper", { path, which }, { btn: e.currentTarget, ok: "Wallpaper changed" });
              } }, icon("image"), "Set")))),
        h("div.card", null, h("div.card-title", null, icon("info"), "About the phone"), facts)),
      h("div.card", { style: { marginTop: "16px" } }, h("div.card-title", null, icon("gauge"), "Sensors"), sensors));

    const row = (k, v) => [h("dt", null, k), h("dd", null, v == null || v === "" ? "–" : String(v))];
    (async () => {
      try {
        const d = await op("device.info");
        ringer.set(d.ringer);
        dnd.set(d.dnd);
        btState.textContent = d.bluetooth == null ? "Unknown" : d.bluetooth ? "On" : "Off";
        if (!d.can.bluetooth) { btState.textContent += " · on Android 13+ only the user can switch it"; }
        if (d.screen_timeout_s) {
          const v = String(d.screen_timeout_s);
          if (![...timeoutSel.options].some((o) => o.value === v)) timeoutSel.append(h("option", { value: v }, d.screen_timeout_s < 60 ? v + " s" : Math.round(d.screen_timeout_s / 60) + " min"));
          timeoutSel.value = v;
        }
        clear(facts, [
          ...row("Model", `${d.manufacturer} ${d.model}`), ...row("Android", `${d.android} (API ${d.sdk})`),
          ...row("Security patch", d.security_patch), ...row("Build", d.build),
          ...row("Screen", `${d.screen.width} × ${d.screen.height} · ${d.screen.dpi} dpi`),
          ...row("CPU", (d.abis || []).join(", ")), ...row("Up", fmtUptime(d.uptime_s)),
        ]);
      } catch (e) { clear(facts, h("div.muted", null, errText(e))); }
      try {
        const list = await op("device.sensors");
        const seen = new Set();
        clear(sensors, list.length ? list.filter((s) => !seen.has(s.type) && seen.add(s.type)).map((s) => {
          const val = h("span.num.muted", null, "");
          return h("div.set-row", null, h("div.txt", null, h("b", null, s.name), h("small", null, `${s.kind}${s.vendor ? " · " + s.vendor : ""}`)), val,
            h("button.btn.sm", { onclick: async (e) => {
              const r = await run("device.sensor", { type: s.type }, { btn: e.currentTarget });
              if (r) val.textContent = r.values.slice(0, 3).map((x) => Math.round(x * 100) / 100).join(" · ") + (r.unit ? " " + r.unit : "");
            } }, "Read"));
        }) : h("div.muted", null, "No sensors reported."));
      } catch (e) { clear(sensors, h("div.muted", null, errText(e))); }
    })();
    return null;
  },
};
