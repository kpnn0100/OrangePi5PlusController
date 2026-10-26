// Gallery (GAL-01..12): takes grouped by month and day, their variants, playback (GAL-11),
// download - one file, or many at once as one ZIP (GAL-12) - convert, delete, jobs.

import { h, clear, icon, store, conn, run, sheet, confirmBox, toast, toggle, fmtBytes, fmtDur, busy, errText } from "../core.js";
import { FilePlayer, streamMode } from "../fileplayer.js";

const KINDS = [["all", "All"], ["RAW", "RAW"], ["H.265", "H.265"], ["H.264", "H.264"], ["FFV1", "FFV1"], ["VIDEO", "Other"]];
const KIND_TAG = { RAW: "", "H.265": "accent", "H.264": "ok", FFV1: "warn", VIDEO: "" };
let targetsCache = null;

/** The browser plays the file itself (HTTP Range: smooth seeking). H.265 never: recordings
 *  carry PCM audio browsers refuse, and a 4K H.265 file is too big a download - it streams. */
function native(item) {
  if (!item || item.recording || item.problem || item.codec === "hevc") return false;
  const ext = item.id.split(".").pop().toLowerCase();
  const v = document.createElement("video");
  if (!["mp4", "mov", "m4v", "webm", "mkv"].includes(ext)) return false;
  if (item.codec === "h264") return ext !== "mkv" && !!v.canPlayType('video/mp4; codecs="avc1.640033"');
  if (["vp9", "vp8", "av1"].includes(item.codec)) return !!v.canPlayType("video/webm");
  return false;
}

/** Natively, or streamed by the Pi (GAL-11: H.265 as it is or converted, H.264 in any container). */
function playable(item) {
  return native(item) || !!streamMode(item);
}

function bestPlayable(take) {
  const order = ["H.264", "VIDEO", "H.265"];
  return [...take.items].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind)).find(playable) || null;
}

/** "27 Sep 2026  22:52:52" -> ["27 Sep 2026", "22:52:52"] */
function titleParts(take) {
  const m = /^(.*\S)\s{2,}(\S+)$/.exec(take.title || "");
  return m ? [m[1], m[2]] : [take.title, null];
}

function thumbUrl(take) {
  const v = Math.max(0, ...take.items.map((i) => i.mtime || 0));
  return `/api/thumb/${encodeURIComponent(take.id)}?v=${v}`;
}

function mediaUrl(item, download) {
  return `/api/media/${encodeURIComponent(item.id)}` + (download ? "?download=1" : "");
}

function kindTag(kind) {
  return h("span.tag", { class: KIND_TAG[kind] || "" }, kind === "VIDEO" ? "Video" : kind);
}

function itemMeta(i) {
  return [i.width ? `${i.width}×${i.height}` : null, i.fps ? `${Math.round(i.fps * 100) / 100} fps` : null,
          i.duration ? fmtDur(i.duration) : null, fmtBytes(i.size), i.pixel_format || null,
          i.audio ? "audio" : null].filter(Boolean).join(" · ");
}

async function targets() {
  if (!targetsCache) targetsCache = (await conn.call("gallery.targets")).targets;
  return targetsCache;
}

/** "2026-09" -> "September 2026"; "2026-09-30" -> "Wed 30 Sep" (the board's date, GAL-01) - the
 *  words the app and the CLI use too. */
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December"];
function monthLabel(m) {
  const [y, mo] = m.split("-").map(Number);
  return `${MONTHS[mo - 1]} ${y}`;
}
function dayLabel(d) {
  const [y, mo, da] = d.split("-").map(Number);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(y, mo - 1, da).getDay()];
  return `${wd} ${da} ${MONTHS[mo - 1].slice(0, 3)}`;
}
function dayOf(t) {
  if (t.day) return t.day;
  const d = new Date((t.created || 0) * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Takes grouped by month, then by day, in list order (newest first). */
function groupTakes(takes) {
  const months = [];
  for (const t of takes) {
    const day = dayOf(t), month = day.slice(0, 7);
    let m = months[months.length - 1];
    if (!m || m.key !== month) months.push(m = { key: month, days: [] });
    let d = m.days[m.days.length - 1];
    if (!d || d.key !== day) m.days.push(d = { key: day, takes: [] });
    d.takes.push(t);
  }
  return months;
}

/** The files of `takes` that a download would carry under the kind filter (GAL-12). */
function filesOf(takes, kind) {
  const out = [];
  for (const t of takes) for (const i of t.items) if (kind === "all" || i.kind === kind) out.push(i);
  return out;
}

function checkbox(onToggle, label) {
  const b = h("button.selbox", { type: "button", role: "checkbox", "aria-checked": "false", "aria-label": label,
                                 onclick: (e) => { e.stopPropagation(); onToggle(); } }, icon("check"));
  b.set = (state) => {           // true | false | "mixed"
    b.setAttribute("aria-checked", state === "mixed" ? "mixed" : state ? "true" : "false");
  };
  return b;
}

export default {
  id: "gallery", title: "Gallery", icon: "gallery",

  mount(root) {
    let filter = "all";
    let takes = null;
    let loading = false;
    let version = -1;
    let selecting = false;
    const selected = new Set();                 // take ids - this page's own choice, never shared
    let refs = { cards: new Map(), groups: [] };

    const sub = h("div.sub");
    const chips = h("div.chips");
    const selectBtn = h("button.btn.sm.ghost", { onclick: () => setSelecting(!selecting) });
    const jobsBox = h("div.card.hidden", { style: { marginBottom: "18px" } });
    const grid = h("div.gallery");
    const empty = h("div.empty.hidden");
    const barInfo = h("div.grow.ellipsis");
    const dlBtn = h("button.btn.sm.primary", { onclick: (e) => downloadSelected(e.currentTarget) }, icon("download"), "Download ZIP");
    const bar = h("div.selbar", { "aria-live": "polite" }, barInfo,
      h("button.btn.sm.ghost", { onclick: () => { selected.clear(); sync(); } }, "Clear"), dlBtn);
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Gallery"), sub), h("div.actions.chip-row", null, chips, selectBtn)),
      jobsBox, grid, empty, bar);

    const renderChips = () => clear(chips, KINDS.map(([k, label]) =>
      h("button.chip", { class: k === filter ? "active" : "", onclick: () => { filter = k; renderChips(); renderTakes(); } }, label)));

    function setSelecting(on) {
      selecting = on;
      if (!on) selected.clear();
      root.classList.toggle("selecting", on);
      selectBtn.replaceChildren(on ? "Done" : "Select");
      sync();
    }

    function shownTakes() {
      return !takes ? [] : filter === "all" ? takes : takes.filter((t) => t.items.some((i) => i.kind === filter));
    }

    function renderTakes() {
      if (!takes) {
        clear(grid, h("div.takes", null, [0, 1, 2].map(() => h("div.card.take", { style: { opacity: ".5" } }, h("div.thumb"),
                                                         h("div.take-body", null, h("div.take-title", null, " "))))));
        return;
      }
      const shown = shownTakes();
      for (const id of [...selected]) if (!shown.some((t) => t.id === id)) selected.delete(id);   // gone or filtered out
      empty.classList.toggle("hidden", shown.length > 0);
      clear(empty, icon("film"), h("div", null, takes.length ? "Nothing in this format." : "No recordings yet."),
            !takes.length ? h("div.hint", null, "Recordings appear here as soon as they start.") : null);
      refs = { cards: new Map(), groups: [] };
      let n = 0;
      clear(grid, groupTakes(shown).map((m) => {
        const all = m.days.flatMap((d) => d.takes);
        const mBox = checkbox(() => toggleGroup(all), "Select " + monthLabel(m.key));
        refs.groups.push({ box: mBox, takes: all });
        return h("section.month", null,
          h("div.group-head.month-head", null, mBox, h("h2", null, monthLabel(m.key)), groupMeta(all)),
          m.days.map((d) => {
            const dBox = checkbox(() => toggleGroup(d.takes), "Select " + dayLabel(d.key));
            refs.groups.push({ box: dBox, takes: d.takes });
            return h("div.day", null,
              h("div.group-head.day-head", null, dBox, h("h3", null, dayLabel(d.key)), groupMeta(d.takes)),
              h("div.takes", null, d.takes.map((t) => takeCard(t, n++))));
          }));
      }));
      sync();
    }

    function groupMeta(ts) {
      const size = ts.reduce((a, t) => a + t.size, 0);
      return h("span.group-meta", null, `${ts.length} take${ts.length === 1 ? "" : "s"} · ${fmtBytes(size)}`);
    }

    function toggleGroup(ts) {
      if (!selecting) setSelecting(true);
      const on = !ts.every((t) => selected.has(t.id));
      for (const t of ts) on ? selected.add(t.id) : selected.delete(t.id);
      sync();
    }

    function toggleTake(id) {
      selected.has(id) ? selected.delete(id) : selected.add(id);
      sync();
    }

    /** Selection state -> cards, group boxes and the bar, in place (no re-render, no reload). */
    function sync() {
      for (const [id, el] of refs.cards) {
        el.classList.toggle("selected", selected.has(id));
        el.querySelector(".selbox").set(selected.has(id));
      }
      for (const g of refs.groups) {
        const k = g.takes.filter((t) => selected.has(t.id)).length;
        g.box.set(k === 0 ? false : k === g.takes.length ? true : "mixed");
      }
      const chosen = (takes || []).filter((t) => selected.has(t.id));
      const files = filesOf(chosen, filter);
      const ready = files.filter((i) => !i.recording && !i.busy);
      const size = ready.reduce((a, i) => a + i.size, 0);
      bar.classList.toggle("on", selecting);
      barInfo.textContent = !chosen.length ? "Choose takes, a day or a month"
        : `${chosen.length} take${chosen.length === 1 ? "" : "s"} · ${ready.length} file${ready.length === 1 ? "" : "s"}` +
          `${filter === "all" ? "" : " " + (filter === "VIDEO" ? "other" : filter)} · ${fmtBytes(size)}` +
          (ready.length < files.length ? ` · ${files.length - ready.length} still being written` : "");
      dlBtn.disabled = !ready.length;
    }

    async function downloadSelected(btn) {
      const b = await busy(btn, () => conn.call("gallery.bundle", { takes: [...selected], kind: filter }));
      if (!b) return;
      // The board streams ONE stored ZIP with its length known, so the browser shows real progress.
      const a = h("a", { href: b.url, download: b.name });
      document.body.append(a);
      a.click();
      a.remove();
      toast(`Downloading ${b.name} · ${b.files.length} file${b.files.length === 1 ? "" : "s"} · ${fmtBytes(b.size)}` +
            (b.skipped.length ? ` · ${b.skipped.length} left out (still being written)` : ""), "ok");
    }

    function takeCard(t, n) {
      const readable = t.items.some((i) => !String(i.problem || "").startsWith("can't"));
      const img = readable ? h("img", { alt: "", loading: "lazy", decoding: "async", src: thumbUrl(t) }) : null;
      const ph = h("div.ph", null, icon(readable ? "film" : "alert"));
      if (img) {
        img.onload = () => { img.classList.add("loaded"); ph.remove(); };
        img.onerror = () => img.remove();
      }
      const open = () => (selecting ? toggleTake(t.id) : openTake(t.id));
      const card = h("div.card.take", { style: { animationDelay: Math.min(n, 12) * 30 + "ms" }, onclick: open,
                                        role: "button", tabindex: "0", onkeydown: (e) => e.key === "Enter" && open() },
        h("div.thumb", null, ph, img,
          checkbox(() => { if (!selecting) setSelecting(true); toggleTake(t.id); }, "Select " + t.title),
          t.recording ? h("span.flag.tag.rec.live", null, h("span.dot"), "REC") : null,
          t.duration ? h("span.dur", null, fmtDur(t.duration)) : null),
        h("div.take-body", null,
          h("div.take-title.ellipsis", null, titleParts(t)[1] || t.title),
          h("div.take-meta.ellipsis", null, [titleParts(t)[1] ? titleParts(t)[0] : null,
            `${t.items.length} file${t.items.length === 1 ? "" : "s"}`, fmtBytes(t.size)].filter(Boolean).join(" · ")),
          h("div.tags", null, t.kinds.map(kindTag))));
      refs.cards.set(t.id, card);
      return card;
    }

    async function load() {
      if (loading) return;
      loading = true;
      try {
        const r = await conn.call("gallery.list");
        takes = r.takes;
        version = r.version;
        renderTakes();
      } catch (e) {
        toast(errText(e), "err");
      } finally {
        loading = false;
      }
    }

    function renderJobs(jobs) {
      jobs = jobs || [];
      jobsBox.classList.toggle("hidden", !jobs.length);
      if (!jobs.length) return;
      const anyDone = jobs.some((j) => ["done", "failed", "cancelled"].includes(j.state));
      clear(jobsBox,
        h("div.card-title", null, "Conversions",
          h("div.actions", null, anyDone ? h("button.btn.ghost.sm", { onclick: (e) => run("jobs.clear", {}, { btn: e.currentTarget }) },
                                              "Clear finished") : null)),
        h("div", null, jobs.map(jobRow)));
    }

    renderChips();
    setSelecting(false);
    renderTakes();
    const offs = [
      store.on("gallery", (g) => {
        sub.textContent = `${g.takes} take${g.takes === 1 ? "" : "s"} · ${fmtBytes(g.size)} · ${g.folder}`;
        if (g.version !== version) load();
      }),
      store.on("jobs", renderJobs),
    ];
    if (!store.get("gallery")) load();
    return () => offs.forEach((f) => f());
  },
};

export function jobRow(j) {
  const finished = ["done", "failed", "cancelled"].includes(j.state);
  const pct = Math.round((j.progress || 0) * 100);
  const v = j.verify;
  const verdict = v ? (v.ok ? h("span.tag.ok", null, v.raw_deleted ? "Verified · RAW deleted" : "Verified")
                            : h("span.tag.rec", null, "Not identical")) : null;
  const stateTag = { queued: h("span.tag", null, "Queued"), running: h("span.tag.accent", null, pct + "%"),
                     verifying: h("span.tag.accent", null, `Checking ${pct}%`),
                     done: verdict || h("span.tag.ok", null, "Done"), failed: verdict || h("span.tag.rec", null, "Failed"),
                     retrying: h("span.tag.warn", null, `Retry ${j.attempt}/${j.max_attempts}`),
                     cancelled: h("span.tag", null, "Cancelled") }[j.state];
  const detail = j.state === "running" || j.state === "verifying"
    ? [j.state === "verifying" ? "comparing every frame with the RAW" : null,
       j.fps ? `${Math.round(j.fps)} fps` : null, j.eta !== null && j.eta !== undefined ? `${fmtDur(j.eta)} left` : null,
       j.live ? "following the recording" : null,
       j.attempt > 1 ? `attempt ${j.attempt} of ${j.max_attempts}` : null].filter(Boolean).join(" · ")
    : j.state === "retrying" ? `failed: ${j.error || "?"} · trying again shortly`
    : v ? (v.ok ? v.detail + (v.raw_deleted ? " · the FFV1 is now the original" : "") : v.detail + " · the RAW is kept")
    : j.error || "";
  return h("div.job", null,
    h("div.top", null,
      h("div.grow", null,
        h("div.ellipsis", { style: { fontWeight: 550, fontSize: "14px" } }, j.output),
        h("div.muted.ellipsis", { style: { fontSize: "12.5px" } },
          j.codec === "verify" ? `${j.title} · against ${j.source}` : `${j.title} · from ${j.source}`)),
      stateTag,
      !finished ? h("button.btn.ghost.icon.sm", { title: "Cancel", "aria-label": "Cancel conversion",
                                                  onclick: (e) => run("jobs.cancel", { id: j.id }, { btn: e.currentTarget }) }, icon("x")) : null),
    !finished ? h("div.bar", null, h("i", { style: { width: pct + "%" } })) : null,
    detail ? h("div", { style: { fontSize: "12.5px", color: j.state === "failed" || (v && !v.ok) ? "var(--err)" : "var(--muted)" } }, detail) : null,
    // GAL-09/10: repaired RAW, skipped damage, retries and resumes
    j.note ? h("div.dim", { style: { fontSize: "12px" } }, j.note) : null);
}

// ---------------------------------------------------------------- take
export function openTake(takeId) {
  let take = null;
  let playing = null;
  let fp = null;                                     // the streamed player (GAL-11), if one is shown
  const player = h("div.player");
  const list = h("div");
  const footDel = h("button.btn.danger", { onclick: () => deleteTake() }, icon("trash"), "Delete take");
  const s = sheet({ title: "Recording", body: h("div", null, player, list), foot: [footDel], wide: true });

  async function refresh() {
    try {
      take = await conn.call("gallery.get", { take: takeId });
    } catch (e) {
      if (!s.closed) { toast("This recording is gone", "err"); s.close(); }
      return;
    }
    if (s.closed) return;
    s.setTitle(take.title);
    if (playing && !take.items.some((i) => i.id === playing)) playing = null;
    renderPlayer();
    renderList();
  }

  function renderPlayer() {
    const current = playing && take.items.find((i) => i.id === playing);
    const item = current || bestPlayable(take);
    const cur = player.querySelector("video");
    if (item && cur && cur.dataset.id === item.id) return;     // keep playback position
    fp?.destroy();
    fp = null;
    player.classList.remove("fp");
    if (item) {
      playing = item.id;
      if (native(item)) {
        clear(player, h("video", { src: mediaUrl(item), controls: true, playsInline: true, preload: "metadata",
                                   "data-id": item.id, poster: thumbUrl(take) }));
      } else {
        clear(player);
        // nothing this device decodes (e.g. a phone and 4K): offer a smaller copy it plays itself
        const onCantPlay = (big) => player.querySelector(".note")?.append(
          h("button.btn.sm.primary", { onclick: () => openConvert(item, "h264-vpu", big ? { scale: "1080" } : {}) },
            icon("convert"), big ? "Make 1080p H.264" : "Make H.264"));
        fp = new FilePlayer(player, item, { poster: thumbUrl(take), onCantPlay });
      }
    } else {
      const src = take.items.find((i) => !i.recording && i.kind !== "H.264");
      clear(player, h("img", { src: thumbUrl(take), alt: "" }),
        h("div.note", null, icon("info"), h("span.grow", null, take.recording ? "Still recording." :
          "No copy this browser can play. Make an H.264 copy to watch it here."),
          src && !take.recording ? h("button.btn.sm.primary", { onclick: () => openConvert(src, "h264-vpu") }, icon("convert"), "Make H.264") : null));
    }
    renderList();
  }

  function renderList() {
    clear(list, h("div.section-title", null, "Files", h("span.muted", { style: { fontWeight: 500 } },
                  `${take.items.length} · ${fmtBytes(take.size)}`)),
      take.items.map((i) => {
        const canPlay = playable(i);
        const locked = i.recording || i.busy;
        return h("div.variant", { class: i.id === playing ? "playing" : "" },
          h("div.info", null,
            h("div.name", null, kindTag(i.kind), h("span.ellipsis", null, i.id),
              i.recording ? h("span.tag.rec.live", null, h("span.dot"), "Recording") : null,
              i.busy && !i.recording ? h("span.tag.accent", null, "In use") : null,
              i.verified ? h("span.tag.ok", { title: `identical to ${i.verified.raw} (${i.verified.frames} frames)` }, "Verified lossless") : null),
            h("div.meta", null, itemMeta(i)),
            i.problem ? h("div.problem", null, i.problem) : null,
            i.note ? h("div.dim", { style: { fontSize: "12px" } }, i.note) : null),
          h("div.acts", null,
            canPlay ? h("button.btn.ghost.icon.sm", { title: "Play", "aria-label": "Play",
                                                      onclick: () => { playing = i.id; player.replaceChildren(); renderPlayer(); } }, icon("play")) : null,
            h("a.btn.ghost.icon.sm", { href: mediaUrl(i, true), download: i.id, title: "Download", "aria-label": "Download" }, icon("download")),
            h("button.btn.ghost.icon.sm", { title: "Convert", "aria-label": "Convert", disabled: i.recording,
                                            onclick: () => openConvert(i) }, icon("convert")),
            i.kind === "FFV1" && take.items.some((x) => x.kind === "RAW") ?
              h("button.btn.ghost.icon.sm", { title: "Check against the RAW", "aria-label": "Check against the RAW",
                                              disabled: locked, onclick: () => verifyItem(i) }, icon("check")) : null,
            h("button.btn.ghost.icon.sm.danger", { title: locked ? "In use" : "Delete this file", "aria-label": "Delete",
                                                   disabled: locked, onclick: () => deleteItem(i) }, icon("trash"))));
      }));
    footDel.disabled = take.items.some((i) => i.recording || i.busy);
  }

  function verifyItem(i) {
    const raw = take.items.find((x) => x.kind === "RAW");
    const go = async (deleteRaw, btn) => {
      const job = await busy(btn, () => conn.call("gallery.verify", { file: i.id, delete_raw: deleteRaw }));
      if (job) { toast("Checking " + i.id + " against the RAW", "ok"); s2.close(); }
    };
    const s2 = sheet({
      title: "Check the FFV1 copy",
      body: h("div.stack", null,
        h("p.dim", { style: { margin: 0 } }, `Every frame and audio sample of ${i.id} is compared byte for byte with ${raw.id}.`),
        h("p.muted", { style: { margin: 0, fontSize: "13px" } }, `${fmtBytes(raw.size)} could be freed. If anything differs, nothing is deleted.`)),
      foot: [h("button.btn.ghost", { onclick: (e) => go(false, e.currentTarget) }, "Check only"),
             h("button.btn.danger.solid", { onclick: (e) => go(true, e.currentTarget) }, "Check, then delete RAW")],
    });
  }

  async function deleteItem(i) {
    const others = take.items.length - 1;
    const ok = await confirmBox({ title: "Delete this file?", danger: true, ok: "Delete",
      text: `${i.id} (${fmtBytes(i.size)}) will be removed from the Pi.` + (others ? ` The other ${others} file${others === 1 ? "" : "s"} of this take stay.` : "") });
    if (!ok) return;
    if (await run("gallery.delete", { file: i.id }, { ok: "Deleted" })) {
      if (others === 0) s.close(); else refresh();
    }
  }

  async function deleteTake() {
    const ok = await confirmBox({ title: "Delete the whole take?", danger: true, ok: "Delete all",
      text: `All ${take.items.length} files (${fmtBytes(take.size)}) of ${take.title} will be removed from the Pi.` });
    if (ok && await run("gallery.delete_take", { take: take.id }, { ok: "Take deleted" })) s.close();
  }

  const off = store.on("gallery", () => refresh(), { now: false });
  const origClose = s.close;
  s.close = () => { off(); fp?.destroy(); player.querySelector("video")?.pause(); origClose(); };
  refresh();
  return s;
}

// -------------------------------------------------------------- convert
export async function openConvert(item, preselect, preset = {}) {
  let list;
  try { list = await targets(); } catch (e) { toast(errText(e), "err"); return; }
  const caps = new Set(list.filter((t) => t.available).map((t) => t.id));
  let sel = preselect && caps.has(preselect) ? preselect : (list.find((t) => t.available && !sameKind(t.id, item)) || {}).id;
  const rawDefault = ((store.get("recorder.settings") || {}).raw || {}).ffv1_replace_raw;
  const opts = { quality: "high", scale: preset.scale || "source", preset: "fast", bitrate: "", replaceRaw: rawDefault !== false };
  const box = h("div");
  const startBtn = h("button.btn.primary", { onclick: () => start() }, icon("convert"), "Convert");
  const s = sheet({ title: "Convert", body: box, foot: [h("button.btn.ghost", { onclick: () => s.close() }, "Cancel"), startBtn] });

  function sameKind(id, it) {
    return ({ "h264-vpu": "H.264", "h265-vpu": "H.265", "h265-x265": "H.265", ffv1: "FFV1", "ffv1-gpu": "FFV1" })[id] === it.kind;
  }

  function render() {
    const t = list.find((x) => x.id === sel);
    const o = (t && t.options) || {};
    const selectRow = (label, key, values) => h("div.field", null, h("label", null, label),
      h("select.input", { onchange: (e) => { opts[key] = e.target.value; } },
        values.map(([v, l]) => h("option", { value: v, selected: opts[key] === v }, l))));
    clear(box,
      h("div.muted", { style: { fontSize: "13px", marginBottom: "12px" } }, "From ", h("span.mono", null, item.id), " · ", itemMeta(item)),
      list.map((x) => h("div.target", { class: [x.id === sel ? "sel" : "", x.available ? "" : "off"].join(" "),
                                        onclick: () => { sel = x.id; render(); } },
        h("span.radio"), h("div", null, h("b", null, x.title),
          h("small", null, x.available ? x.description : "Not available on this board")))),
      t ? h("div.grid", { style: { gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", marginTop: "16px" } },
        o.scale ? selectRow("Size", "scale", o.scale.map((v) => [v, v === "source" ? "Same as source" : v + "p"])) : null,
        o.quality ? selectRow("Quality", "quality", o.quality.map((v) => [v, v[0].toUpperCase() + v.slice(1)])) : null,
        o.preset ? selectRow("x265 preset", "preset", o.preset.map((v) => [v, v])) : null,
        o.bitrate ? h("div.field", null, h("label", null, "Bitrate (Mb/s)"),
          h("input.input", { type: "number", min: "0.1", max: String(o.bitrate_max || 1000), step: "any",
                             placeholder: o.bitrate_max ? `auto (quality) · max ${o.bitrate_max}` : "auto (quality)", value: opts.bitrate,
                             oninput: (e) => { opts.bitrate = e.target.value; } })) : null) : null,
      t && sel.startsWith("ffv1") && item.kind === "RAW" ? h("label.row", { style: { marginTop: "14px", cursor: "pointer" } },
        toggle(opts.replaceRaw, (on) => { opts.replaceRaw = on; }),
        h("div", null, h("b", null, "Delete the RAW when the copy is verified"),
          h("div.hint", null, "Every frame and audio sample is compared byte for byte first"))) : null);
    startBtn.disabled = !t || !t.available;
  }

  async function start() {
    const args = { file: item.id, target: sel, quality: opts.quality, scale: opts.scale, preset: opts.preset };
    if (sel.startsWith("ffv1") && item.kind === "RAW") args.replace_raw = opts.replaceRaw;
    if (opts.bitrate) args.bitrate = Number(opts.bitrate);
    const job = await busy(startBtn, () => conn.call("gallery.convert", args));
    if (job) { toast("Conversion started: " + job.output, "ok"); s.close(); }
  }
  render();
}
