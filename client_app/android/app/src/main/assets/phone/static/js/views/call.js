// Call (PHN-06): an internet voice call between this browser and the person holding the phone.
// call.start makes the phone ring (full screen, also locked); ws/call carries the call: phone →
// here {"t": "state", state, reason?} and binary PCM (16 kHz s16le, 20 ms); here → phone binary PCM
// from the browser's microphone (echo cancellation on) and {"t": "end"}. The phone's front camera
// can be watched alongside (ws/camera?facing=front). The call keeps going while you use other pages.

import { h, clear, icon, store, op, toast, errText, pref, featureReady, featureReason, fmtDur, wsUrl, toggle } from "../core.js";
import { MicCapture, PcmPlayer, meter } from "../audio.js";
import { VideoView } from "../video.js";

// ------------------------------------------------------------ the call session (survives page changes)
const session = {
  state: "idle",              // idle | starting | ringing | active | ended
  reason: "",
  since: 0,
  muted: false,
  ws: null,
  mic: null,
  player: null,
  sent: 0,                    // frames sent / received (tests, stats)
  got: 0,
  subs: new Set(),
  levels: { mic: 0, phone: 0 },
  volume: pref.get("call.volume", 1),
};
const emit = () => {
  session.setBadge?.("call", session.state === "active" || session.state === "ringing" ? 1 : 0);
  for (const f of [...session.subs]) { try { f(); } catch (e) { console.error(e); } }
};
window.phoneDebug = window.phoneDebug || {};
window.phoneDebug.call = session;

function setState(state, reason = "") {
  if (session.state === state && session.reason === reason) return;
  session.state = state;
  session.reason = reason;
  if (state === "active" && !session.since) session.since = Date.now();
  emit();
}

async function startCall(name) {
  if (["starting", "ringing", "active"].includes(session.state)) return;
  session.sent = session.got = 0;
  session.since = 0;
  session.muted = false;
  setState("starting");
  try {
    session.player = new PcmPlayer({ onLevel: (v) => { session.levels.phone = v; session.onLevel?.(); } });
    session.player.setVolume(session.volume);
    const mic = new MicCapture({
      onFrame: (f) => {
        const ws = session.ws;
        if (session.state === "active" && ws && ws.readyState === 1) { ws.send(f); session.sent++; }
      },
      onLevel: (v) => { session.levels.mic = v; session.onLevel?.(); },
    });
    session.mic = mic;
    await mic.start();
    if (session.state !== "starting") { mic.stop(); return; }         // hung up meanwhile
    const r = await op("call.start", name ? { name } : {});
    if (session.state !== "starting") return;
    setState((r && r.state) || "ringing");
    openSocket();
  } catch (e) {
    cleanup();
    setState("ended", errText(e));
    toast(errText(e), "err");
  }
}

function openSocket() {
  const ws = new WebSocket(wsUrl("ws/call"));
  ws.binaryType = "arraybuffer";
  session.ws = ws;
  ws.onmessage = (e) => {
    if (session.ws !== ws) return;
    if (typeof e.data === "string") {
      let m;
      try { m = JSON.parse(e.data); } catch (x) { return; }
      if (m.t === "state") {
        if (m.state === "ended") { endLocal(m.reason || "The call ended"); return; }
        setState(m.state, m.reason || "");
      } else if (m.t === "config" && m.rate) session.player?.setRate(m.rate);
      else if (m.t === "error" && m.error) toast(m.error, "err");
    } else {
      session.got++;
      session.player?.push(e.data);
    }
  };
  ws.onclose = (e) => {
    if (session.ws !== ws) return;
    session.ws = null;
    if (["ringing", "active", "starting"].includes(session.state)) endLocal(e.reason || "The call was cut");
  };
}

function cleanup() {
  const ws = session.ws;
  session.ws = null;
  if (ws) { try { ws.close(); } catch (e) { /* gone */ } }
  session.mic?.stop();
  session.mic = null;
  session.player?.close();
  session.player = null;
  session.levels = { mic: 0, phone: 0 };
}

function endLocal(reason) {
  cleanup();
  setState("ended", reason);
}

export function hangUp() {
  const ws = session.ws;
  const was = session.state;
  if (ws && ws.readyState === 1) {
    try { ws.send(JSON.stringify({ t: "end" })); } catch (e) { /* closing */ }
    session.ws = null;
    setTimeout(() => { try { ws.close(); } catch (e) { /* gone */ } }, 300);
  } else if (was === "ringing" || was === "active" || was === "starting") {
    op("call.end").catch(() => {});
  }
  endLocal(was === "active" ? "You hung up" : "Cancelled");
}

window.addEventListener("pagehide", () => { if (session.ws) hangUp(); });

// ------------------------------------------------------------------------------ the page
export default {
  mount(root, { setBadge }) {
    const nameIn = h("input.input", { type: "text", value: pref.get("call.name", ""), placeholder: "Your name (shown on the phone)",
      "aria-label": "Your name", maxlength: "40", style: { maxWidth: "320px" },
      onchange: () => pref.set("call.name", nameIn.value.trim()) });
    const avatar = h("div.call-avatar", null, icon("call"));
    const stateEl = h("div.call-state");
    const subEl = h("div.call-sub");
    const btns = h("div.call-btns");
    const micMeter = meter(h, "Your microphone");
    const phoneMeter = meter(h, "The phone");
    const vol = h("input", { type: "range", min: "0", max: "1.5", step: "0.05", value: String(session.volume), "aria-label": "Call volume",
      oninput: () => { session.volume = Number(vol.value); session.player?.setVolume(session.volume); pref.set("call.volume", session.volume); } });
    const meters = h("div.call-meters", null, h("span", null, "You"), micMeter, h("span", null, "Phone"), phoneMeter,
                     h("span", null, "Volume"), vol);
    const nameRow = h("div.row", { style: { justifyContent: "center", width: "100%" } }, nameIn);
    const card = h("div.card.call-card", null, avatar, stateEl, subEl, nameRow, btns, meters,
      h("div.hint", null, "Use headphones for the best sound. The browser asks for the microphone the first time."));

    // the phone's front camera alongside (PHN-06)
    const camOk = featureReady("camera");
    let camView = null;
    const camStage = h("div.stage.call-cam", null, h("div.msg", null, h("span.spin"), h("b", null, "Opening the front camera…")));
    const camPane = h("div.hidden", null, camStage);
    const camToggle = toggle(pref.get("call.cam", false) && camOk, (on) => { pref.set("call.cam", on); setCam(on); },
                             { disabled: !camOk, label: "Watch the phone's front camera" });
    const camRow = h("div.set-row", null, h("div.txt", null, h("b", null, "Watch the phone's front camera"),
      h("small", null, camOk ? "See the person you are talking to" : featureReason("camera"))), camToggle);
    function setCam(on) {
      camPane.classList.toggle("hidden", !on);
      layout.classList.toggle("solo", !on);
      if (on && !camView) { camView = new VideoView(camStage, { rotate: true }); camView.start("ws/camera?facing=front&size=640x480"); }
      if (!on && camView) { camView.destroy(); camView = null; }
    }

    const layout = h("div.call-layout.solo", null, h("div.stack", null, card, h("div.card", { style: { padding: "4px 18px" } }, camRow)), camPane);
    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Call"), h("div.sub", null, "A voice call with the person holding the phone"))),
                layout);

    let timer = 0;
    function render() {
      const s = session.state;
      card.className = "card call-card " + s;
      nameRow.classList.toggle("hidden", !(s === "idle" || s === "ended"));
      meters.classList.toggle("hidden", !(s === "active" || s === "ringing"));
      const other = store.get("call");
      const busyElsewhere = s === "idle" && other && (other.state === "active" || other.state === "ringing");
      stateEl.textContent = { idle: "Call the phone", starting: "Starting…", ringing: "Ringing…", active: "In call", ended: "Call ended" }[s] || s;
      clear(subEl, s === "ringing" ? "Waiting for someone to answer on the phone"
                 : s === "active" ? fmtDur((Date.now() - session.since) / 1000) + (session.muted ? " · muted" : "")
                 : s === "ended" ? session.reason
                 : busyElsewhere ? "A call is in progress from another browser."
                 : "The phone rings full screen, also when it is locked.");
      clearInterval(timer);
      if (s === "active") timer = setInterval(() => { subEl.textContent = fmtDur((Date.now() - session.since) / 1000) + (session.muted ? " · muted" : ""); }, 1000);
      if (s === "idle" || s === "ended") {
        clear(btns, h("button.btn.green.big", { onclick: () => { pref.set("call.name", nameIn.value.trim()); startCall(nameIn.value.trim()); },
                                                 disabled: busyElsewhere }, icon("call"), s === "ended" ? "Call again" : "Call the phone"));
      } else {
        const muteBtn = h("button.btn.round", { class: session.muted ? "on" : "", "aria-label": session.muted ? "Unmute" : "Mute",
          title: session.muted ? "Unmute" : "Mute", disabled: s !== "active",
          onclick: () => { session.muted = !session.muted; session.mic?.setMuted(session.muted); emit(); } }, icon(session.muted ? "mic-off" : "mic"));
        clear(btns, muteBtn, h("button.btn.red.round", { "aria-label": s === "active" ? "Hang up" : "Cancel", title: s === "active" ? "Hang up" : "Cancel",
                                                         onclick: () => hangUp() }, icon("call-end")));
      }
    }
    session.setBadge = setBadge;
    session.onLevel = () => { micMeter.set(session.levels.mic); phoneMeter.set(session.levels.phone); };
    session.subs.add(render);
    const off = store.on("call", () => render(), { now: false });
    render();
    if (camToggle.input.checked) setCam(true);
    return () => {
      session.subs.delete(render);
      session.onLevel = null;
      off();
      clearInterval(timer);
      if (camView) camView.destroy();
    };
  },
};
