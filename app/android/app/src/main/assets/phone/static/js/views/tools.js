// Audio and tools (PHN-15): volume per stream, media keys, speak a text, vibrate, torch,
// brightness (slider or automatic), open a URL on the phone.

import { h, clear, icon, op, run, toast, errText, toggle, pref, featureReady, featureReason, debounce, store } from "../core.js";

const STREAMS = [["media", "Media", "volume"], ["ring", "Ring", "ring"], ["alarm", "Alarm", "clock"], ["call", "Call", "call"]];

export default {
  mount(root) {
    // ------------------------------------------------------------- volume
    const vols = h("div");
    const volRow = ([stream, label, ic]) => {
      const val = h("span.val.num", null, "…");
      const slider = h("input", { type: "range", min: "0", max: "1", step: "0.01", value: "0", disabled: true, "aria-label": label + " volume" });
      const set = debounce(async () => {
        try {
          const r = await op("tools.volume", { stream, level: Number(slider.value) });
          show(r);
        } catch (e) { toast(errText(e), "err"); }
      }, 200);
      const show = (r) => {
        if (!r) return;
        const lv = Number(r.level);
        slider.value = String(lv);
        slider.disabled = false;
        val.textContent = r.max ? `${Math.round(lv * r.max)}/${r.max}` : Math.round(lv * 100) + "%";
      };
      slider.addEventListener("input", () => { val.textContent = Math.round(Number(slider.value) * 100) + "%"; set(); });
      op("tools.volume", { stream }).then(show).catch((e) => { val.textContent = "–"; slider.title = errText(e); });
      return h("div.vol-row", { "data-stream": stream }, h("span.lbl", null, icon(ic), label), slider, val);
    };
    clear(vols, STREAMS.map(volRow));

    // --------------------------------------------------------- media keys
    const media = (action, ic, label) => h("button.btn", { "aria-label": label, title: label,
      onclick: (e) => run("tools.media", { action }, { btn: e.currentTarget }) }, icon(ic), label);

    // -------------------------------------------------------------- speak
    const speakIn = h("textarea.input", { rows: "3", placeholder: "Text the phone says out loud", "aria-label": "Text to speak" });
    const langIn = h("input.input", { type: "text", placeholder: "Language (e.g. en, vi, de)", "aria-label": "Language", value: pref.get("tools.lang", ""),
                                      style: { maxWidth: "200px" } });

    // ------------------------------------------------------- vibrate etc.
    const msIn = h("input.input", { type: "number", min: "50", max: "10000", step: "50", value: "800", "aria-label": "Vibration in ms", style: { maxWidth: "120px" } });
    const torchOk = featureReady("camera");
    const torch = toggle(!!store.get("torch.local"), async (on) => {
      const r = await run("camera.torch", { on });
      if (r) store.set("torch.local", on); else torch.set(!on);
    }, { label: "Torch", disabled: !torchOk });

    const bright = h("input", { type: "range", min: "0", max: "1", step: "0.01", value: String(pref.get("tools.brightness", 0.5)), "aria-label": "Brightness" });
    const autoB = toggle(pref.get("tools.brightness.auto", false), async (on) => {
      pref.set("tools.brightness.auto", on);
      bright.disabled = on;
      await run("tools.brightness", on ? { level: "auto" } : { level: Number(bright.value) });
    }, { label: "Automatic brightness" });
    bright.disabled = pref.get("tools.brightness.auto", false);
    bright.addEventListener("input", debounce(() => {
      pref.set("tools.brightness", Number(bright.value));
      run("tools.brightness", { level: Number(bright.value) });
    }, 250));

    const urlIn = h("input.input", { type: "url", placeholder: "https://…", "aria-label": "URL to open on the phone" });

    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Tools"), h("div.sub", null, "Sound, media, speech and more on the phone"))),
      h("div.grid.two", null,
        h("div.card", null, h("div.card-title", null, icon("volume"), "Volume"), vols),
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("playpause"), "Media"),
          h("div.media-keys", null, media("previous", "prev", "Previous"), media("play_pause", "playpause", "Play / pause"),
            media("next", "next", "Next"), media("stop", "stop", "Stop")),
          h("div.hint", null, "Controls whatever plays on the phone (music, video, podcasts).")),
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("speak"), "Speak"), speakIn,
          h("div.row.wrap", null, langIn,
            h("button.btn.primary", { onclick: (e) => {
              const text = speakIn.value.trim();
              if (!text) { toast("Write what the phone should say", "err"); return; }
              pref.set("tools.lang", langIn.value.trim());
              run("tools.speak", langIn.value.trim() ? { text, lang: langIn.value.trim() } : { text }, { btn: e.currentTarget, ok: "Speaking" });
            } }, icon("speak"), "Speak"))),
        h("div.card", null, h("div.card-title", null, icon("sliders"), "Phone"),
          h("div.set-row", null, h("div.txt", null, h("b", null, "Brightness"), h("small", null, "Or let the phone choose")),
            h("label.row", { style: { gap: "8px" } }, h("span.dim", { style: { fontSize: "13px" } }, "Auto"), autoB)),
          h("div", { style: { padding: "0 0 10px" } }, bright),
          h("div.set-row", null, h("div.txt", null, h("b", null, "Torch"), h("small", null, torchOk ? "The camera's light" : featureReason("camera"))), torch),
          h("div.set-row", null, h("div.txt", null, h("b", null, "Vibrate"), h("small", null, "Milliseconds")), msIn,
            h("button.btn.sm", { onclick: (e) => run("tools.vibrate", { ms: Number(msIn.value) || 800 }, { btn: e.currentTarget, ok: "Vibrating" }) }, icon("vibrate"), "Vibrate"))),
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("globe"), "Open a URL"),
          h("div.row", null, urlIn, h("button.btn.primary", { onclick: (e) => {
            const u = urlIn.value.trim();
            if (!u) return;
            run("tools.open_url", { url: /^[a-z][a-z0-9+.-]*:/i.test(u) ? u : "https://" + u }, { btn: e.currentTarget, ok: "Opened on the phone" });
          } }, icon("external"), "Open")),
          h("div.hint", null, "Opens in the phone's browser or the app that handles the link."))));
    return null;
  },
};
