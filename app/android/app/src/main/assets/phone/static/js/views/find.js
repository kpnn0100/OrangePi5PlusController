// Find my phone (PHN-10): where the phone is now (map + accuracy), its track of the last days, ring
// it loudly (also on silent / Do Not Disturb), lost mode (lock + a message and a number over the
// lock screen). The map is OpenStreetMap's embed; the track is drawn here (no map library).

import { h, clear, icon, store, op, run, toast, errText, fmtAgo, fmtWhen, toMs, confirmBox, busy, seg, pref } from "../core.js";

function bbox(lat, lon, accuracy) {
  const m = Math.max(250, Math.min(20000, (Number(accuracy) || 50) * 4));
  const dLat = m / 111320;
  const dLon = m / (111320 * Math.max(0.2, Math.cos((lat * Math.PI) / 180)));
  return [lon - dLon, lat - dLat, lon + dLon, lat + dLat].map((v) => v.toFixed(6)).join(",");
}

export function mapUrl(lat, lon, accuracy) {
  return `https://www.openstreetmap.org/export/embed.html?bbox=${bbox(lat, lon, accuracy)}&layer=mapnik&marker=${lat.toFixed(6)},${lon.toFixed(6)}`;
}

export function openUrl(lat, lon) {
  return `https://www.openstreetmap.org/?mlat=${lat.toFixed(6)}&mlon=${lon.toFixed(6)}#map=17/${lat.toFixed(6)}/${lon.toFixed(6)}`;
}

export default {
  mount(root) {
    let now = null;                    // the latest fix
    let shown = null;                  // the fix on the map (now or one of the history)
    let hist = [];
    let range = pref.get("find.range", "1d");

    // ------------------------------------------------------------- map
    const frame = h("iframe", { title: "Map of the phone's location", loading: "lazy", referrerpolicy: "no-referrer",
                                sandbox: "allow-scripts allow-same-origin allow-popups" });
    const mapMsg = h("div.empty.loading", { style: { position: "absolute", inset: "0", justifyContent: "center" } }, h("span.spin"), h("div", null, "Locating…"));
    const mapBox = h("div.map-box", null, mapMsg);
    const where = h("div.row.wrap", { style: { marginTop: "12px", gap: "8px 16px" } });
    const freshBtn = h("button.btn.sm", { onclick: (e) => locate(true, e.currentTarget) }, icon("refresh"), "Refresh");
    const openLink = h("a.btn.sm", { target: "_blank", rel: "noopener noreferrer" }, icon("external"), "Open in maps");
    const mapCard = h("div.card", null,
      h("div.card-title", null, icon("pin"), "Location", h("span.actions", null, freshBtn, openLink)), mapBox, where);

    // ---------------------------------------------------------- history
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "track");
    svg.setAttribute("viewBox", "0 0 600 200");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "The phone's track");
    const histList = h("div.list.hist-list");
    const rangeSeg = seg([["1d", "24 h"], ["7d", "7 days"]], range, (r) => { range = r; pref.set("find.range", r); renderHist(); }, { label: "History range" });
    const histCard = h("div.card", null, h("div.card-title", null, icon("clock"), "History", h("span.actions", null, rangeSeg)),
      svg, h("div.hint", { style: { margin: "8px 0 4px" } }, "A fix every 15 minutes while Find my phone is on, kept 7 days. Click a point to see it on the map."),
      histList);

    // ---------------------------------------------------------- actions
    const ringBtn = h("button.btn.big", { style: { width: "100%" }, onclick: (e) => ring(e.currentTarget) });
    const lostBox = h("div.stack");
    const side = h("div.stack", null,
      h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("ring"), "Ring"),
        h("p.dim", { style: { margin: 0, fontSize: "14px" } }, "Rings loudly for a minute, also when the phone is on silent or Do Not Disturb."), ringBtn),
      h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("lock"), "Lost mode"), lostBox));

    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Find my phone"), h("div.sub", null, "Where the phone is, and help to get it back"))),
                h("div.find-layout", null, h("div.stack", null, mapCard, histCard), side));

    function showOnMap(fix) {
      shown = fix;
      if (!fix || fix.lat === undefined) {
        clear(mapMsg, icon("pin"), h("b", null, "No location yet"), h("div", null, "Press Refresh to ask the phone for a fresh fix."));
        mapMsg.classList.remove("loading");
        frame.remove();
        clear(where);
        openLink.classList.add("disabled");
        return;
      }
      const lat = Number(fix.lat), lon = Number(fix.lon);
      const src = mapUrl(lat, lon, fix.accuracy_m);
      if (frame.getAttribute("src") !== src) frame.setAttribute("src", src);
      if (!frame.isConnected) mapBox.append(frame);
      mapMsg.remove();
      openLink.href = openUrl(lat, lon);
      openLink.classList.remove("disabled");
      const isNow = fix === now;
      clear(where,
        h("span", null, h("b", null, isNow ? "Now" : fmtWhen(fix.time)), h("span.muted", null, " · " + fmtAgo(fix.time))),
        fix.accuracy_m !== undefined ? h("span.dim", null, "±" + Math.round(fix.accuracy_m) + " m") : null,
        fix.provider ? h("span.tag", null, fix.provider) : null,
        h("span.mono.muted", null, `${lat.toFixed(5)}, ${lon.toFixed(5)}`),
        !isNow && now ? h("button.btn.sm", { onclick: () => { showOnMap(now); renderHist(); } }, "Back to now") : null);
      svg.querySelectorAll(".pt.sel").forEach((c) => c.classList.remove("sel"));
    }

    async function locate(fresh, btn) {
      if (fresh) clear(where, h("span.spin"), h("span.dim", null, "Asking the phone for a fresh fix (up to 30 s)…"));
      await busy(btn, async () => {
        try {
          const r = await op("find.locate", fresh ? { fresh: true } : {}, { timeout: fresh ? 45000 : 20000 });
          if (r && r.lat !== undefined) { now = r; showOnMap(now); renderHist(); }
          else showOnMap(null);
        } catch (e) {
          if (!now) showOnMap(null);
          else showOnMap(shown || now);
          throw e;
        }
      });
    }

    async function loadHist() {
      try {
        hist = ((await op("find.history", { limit: 700 })) || []).filter((p) => p && p.lat !== undefined)
          .sort((a, b) => (toMs(a.time) || 0) - (toMs(b.time) || 0));
      } catch (e) {
        clear(histList, h("div.empty", null, h("div", null, errText(e))));
        return;
      }
      renderHist();
    }

    function renderHist() {
      const since = Date.now() - (range === "7d" ? 7 : 1) * 86400000;
      const pts = hist.filter((p) => (toMs(p.time) || 0) >= since);
      if (now && !pts.some((p) => toMs(p.time) === toMs(now.time))) pts.push(now);
      svg.replaceChildren();
      const NS = "http://www.w3.org/2000/svg";
      if (pts.length < 1) {
        const t = document.createElementNS(NS, "text");
        t.setAttribute("x", "300"); t.setAttribute("y", "105"); t.setAttribute("text-anchor", "middle"); t.setAttribute("fill", "currentColor");
        t.setAttribute("opacity", ".5"); t.setAttribute("font-size", "14");
        t.textContent = "No history yet";
        svg.append(t);
        clear(histList);
        return;
      }
      // equirectangular, corrected for latitude, fitted into the box with the right aspect
      const lat0 = pts.reduce((s, p) => s + Number(p.lat), 0) / pts.length;
      const k = Math.cos((lat0 * Math.PI) / 180);
      const xs = pts.map((p) => Number(p.lon) * k), ys = pts.map((p) => -Number(p.lat));
      const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
      const W = 600, H = 200, pad = 18;
      const span = Math.max(maxX - minX, ((maxY - minY) * (W - 2 * pad)) / (H - 2 * pad), 0.0005);
      const s = (W - 2 * pad) / span;
      const ox = (W - (maxX - minX) * s) / 2, oy = (H - (maxY - minY) * s) / 2;
      const P = pts.map((p, i) => [ox + (xs[i] - minX) * s, oy + (ys[i] - minY) * s]);
      const path = document.createElementNS(NS, "polyline");
      path.setAttribute("class", "path");
      path.setAttribute("points", P.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "));
      svg.append(path);
      pts.forEach((p, i) => {
        const c = document.createElementNS(NS, "circle");
        const last = i === pts.length - 1;
        c.setAttribute("class", "pt" + (last ? " now" : "") + (shown && p === shown && !last ? " sel" : ""));
        c.setAttribute("cx", P[i][0].toFixed(1));
        c.setAttribute("cy", P[i][1].toFixed(1));
        c.setAttribute("r", last ? "6" : "3.5");
        const tt = document.createElementNS(NS, "title");
        tt.textContent = fmtWhen(p.time) + (p.accuracy_m !== undefined ? ` · ±${Math.round(p.accuracy_m)} m` : "");
        c.append(tt);
        c.addEventListener("click", () => { showOnMap(p); c.classList.add("sel"); });
        svg.append(c);
      });
      clear(histList, [...pts].reverse().slice(0, 60).map((p) => h("div.list-item.click", { onclick: () => { showOnMap(p); window.scrollTo({ top: 0, behavior: "smooth" }); } },
        h("div.ico", null, icon("pin")),
        h("div.main-txt", null, h("div.title", null, fmtWhen(p.time)),
          h("div.meta.mono", null, `${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)}`)),
        h("div.side-meta", null, p.accuracy_m !== undefined ? "±" + Math.round(p.accuracy_m) + " m" : ""))));
    }

    // ----------------------------------------------------- ring and lost
    let lostShown = null;               // the lost box is rebuilt only when lost mode changes (keeps typed text)
    function renderSide() {
      const f = store.get("find") || {};
      clear(ringBtn, icon(f.ringing ? "stop" : "ring"), f.ringing ? "Stop ringing" : "Ring the phone");
      ringBtn.className = "btn big " + (f.ringing ? "red" : "primary");
      if (lostShown === !!f.lost) return;
      lostShown = !!f.lost;
      if (f.lost) {
        clear(lostBox,
          h("div.note-box.warn", null, icon("lock"), h("span", null, "Lost mode is on: the phone is locked and shows your message over the lock screen.")),
          h("button.btn", { onclick: (e) => lostOff(e.currentTarget) }, icon("unlock"), "Turn lost mode off"));
      } else {
        const msgIn = h("textarea.input", { rows: "3", "aria-label": "Message on the lock screen", placeholder: "This phone is lost. Please call the number below." },
                          pref.get("find.message", ""));
        const contactIn = h("input.input", { type: "tel", "aria-label": "Number to call", placeholder: "A number to call (optional)", inputmode: "tel",
                                             value: pref.get("find.contact", "") });
        clear(lostBox,
          h("p.dim", { style: { margin: 0, fontSize: "14px" } }, "Locks the phone now and shows a message - and a number to call - over the lock screen."),
          h("div.field", null, h("label", null, "Message"), msgIn),
          h("div.field", null, h("label", null, "Contact number"), contactIn),
          h("button.btn.danger.solid", { onclick: (e) => lostOn(e.currentTarget, msgIn.value.trim(), contactIn.value.trim()) }, icon("lock"), "Lock and show the message"));
      }
    }

    async function ring(btn) {
      const ringing = !!(store.get("find") || {}).ringing;
      const r = await run(ringing ? "find.ring_stop" : "find.ring", ringing ? {} : { seconds: 60 }, { btn, ok: ringing ? "Stopped ringing" : "The phone is ringing" });
      if (r) store.set("find", { ...(store.get("find") || {}), ringing: !ringing });
    }

    async function lostOn(btn, message, contact) {
      if (!message) { toast("Write the message to show on the lock screen", "err"); return; }
      pref.set("find.message", message);
      pref.set("find.contact", contact);
      if (!(await confirmBox({ title: "Turn lost mode on?", text: "The phone locks now and shows your message over the lock screen until lost mode is turned off.",
                               ok: "Lock the phone", danger: true }))) return;
      const r = await run("find.lost", contact ? { message, contact } : { message }, { btn, ok: "Lost mode is on" });
      if (r) store.set("find", { ...(store.get("find") || {}), lost: true });
    }

    async function lostOff(btn) {
      const r = await run("find.lost_off", {}, { btn, ok: "Lost mode is off" });
      if (r) store.set("find", { ...(store.get("find") || {}), lost: false });
    }

    const off = store.on("find", renderSide);
    renderSide();
    locate(false).catch((e) => toast(errText(e), "err"));
    loadHist();
    return () => off();
  },
};
