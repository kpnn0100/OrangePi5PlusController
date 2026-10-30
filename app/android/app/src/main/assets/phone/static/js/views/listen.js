// Listen (PHN-07): the phone's microphone, live (ws/mic: 16 kHz s16le PCM, 20 ms frames), with a
// level meter. Played through Web Audio with a small jitter buffer.

import { h, clear, icon, pref, wsUrl, fmtDur, toast } from "../core.js";
import { PcmPlayer, meter, audioContext } from "../audio.js";

export default {
  mount(root) {
    let ws = null, player = null, since = 0, timer = 0, wanted = false, retry = 0;
    const stats = { frames: 0 };
    window.phoneDebug.listen = stats;
    const lvl = meter(h, "Microphone level");
    const stateEl = h("div.call-state", null, "Not listening");
    const subEl = h("div.call-sub", null, "Press Listen to hear what the phone's microphone hears.");
    const btn = h("button.btn.primary.big", { onclick: () => (wanted ? stop() : start()) });
    const vol = h("input", { type: "range", min: "0", max: "2", step: "0.05", value: String(pref.get("listen.volume", 1)), "aria-label": "Volume",
      oninput: () => { pref.set("listen.volume", Number(vol.value)); player?.setVolume(Number(vol.value)); } });
    const card = h("div.card.call-card", null,
      h("div.call-avatar", null, icon("mic")), stateEl, subEl, btn,
      h("div.call-meters", null, h("span", null, "Level"), lvl, h("span", null, "Volume"), vol),
      h("div.hint", null, "Android shows its microphone indicator on the phone while you listen."));
    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Listen"), h("div.sub", null, "The phone's microphone, live"))),
                h("div.call-layout.solo", null, card));

    function render(state, detail = "") {
      card.className = "card call-card " + (state === "live" ? "active" : state === "connecting" ? "ringing" : "");
      stateEl.textContent = { idle: "Not listening", connecting: "Connecting…", live: "Listening", reconnecting: "Reconnecting…" }[state] || state;
      subEl.textContent = detail;
      clear(btn, icon(wanted ? "stop" : "mic"), wanted ? "Stop" : "Listen");
      btn.className = "btn big " + (wanted ? "red" : "primary");
    }

    function start() {
      audioContext();                       // from the click: allowed to play
      wanted = true;
      connect();
    }

    function connect() {
      clearTimeout(retry);
      render("connecting");
      player = player || new PcmPlayer({ onLevel: (v) => lvl.set(v) });
      player.setVolume(Number(vol.value));
      const sock = new WebSocket(wsUrl("ws/mic"));
      sock.binaryType = "arraybuffer";
      ws = sock;
      sock.onmessage = (e) => {
        if (ws !== sock) return;
        if (typeof e.data === "string") {
          let m;
          try { m = JSON.parse(e.data); } catch (x) { return; }
          if (m.t === "config" && m.rate) player.setRate(m.rate);
          else if (m.t === "error" && m.error) toast(m.error, "err");
          return;
        }
        if (!since) {
          since = Date.now();
          clearInterval(timer);
          timer = setInterval(() => render("live", fmtDur((Date.now() - since) / 1000)), 1000);
          render("live", "0:00");
        }
        stats.frames++;
        player.push(e.data);
      };
      sock.onclose = (e) => {
        if (ws !== sock) return;
        ws = null;
        since = 0;
        clearInterval(timer);
        lvl.set(0);
        if (!wanted) return;
        render("reconnecting", e.reason || "");
        retry = setTimeout(() => wanted && connect(), e.reason ? 3000 : 1500);
      };
    }

    function stop() {
      wanted = false;
      clearTimeout(retry);
      clearInterval(timer);
      since = 0;
      if (ws) { const s = ws; ws = null; s.close(); }
      player?.close();
      player = null;
      lvl.set(0);
      render("idle", "Press Listen to hear what the phone's microphone hears.");
    }

    render("idle", "Press Listen to hear what the phone's microphone hears.");
    return () => stop();
  },
};
