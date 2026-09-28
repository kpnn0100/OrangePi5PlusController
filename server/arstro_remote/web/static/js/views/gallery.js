// Gallery (GAL-01..07): takes and their variants, playback, download, convert, delete, jobs.

import { h, clear, icon, store, conn, run, sheet, confirmBox, toast, fmtBytes, fmtDur, busy, errText } from "../core.js";

const KINDS = [["all", "All"], ["RAW", "RAW"], ["H.265", "H.265"], ["H.264", "H.264"], ["FFV1", "FFV1"], ["VIDEO", "Other"]];
const KIND_TAG = { RAW: "", "H.265": "accent", "H.264": "ok", FFV1: "warn", VIDEO: "" };
let targetsCache = null;

function playable(item) {
  if (!item || item.recording || item.problem) return false;
  const ext = item.id.split(".").pop().toLowerCase();
  const v = document.createElement("video");
  if (!["mp4", "mov", "m4v", "webm", "mkv"].includes(ext)) return false;
  if (item.codec === "h264") return ext !== "mkv" && !!v.canPlayType('video/mp4; codecs="avc1.640033"');
  if (item.codec === "hevc") return ext !== "mkv" && !!(v.canPlayType('video/mp4; codecs="hvc1.1.6.L153.B0"') ||
                                                         v.canPlayType('video/mp4; codecs="hev1.1.6.L153.B0"'));
  if (["vp9", "vp8", "av1"].includes(item.codec)) return !!v.canPlayType("video/webm");
  return false;
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

export default {
  id: "gallery", title: "Gallery", icon: "gallery",

  mount(root) {
    let filter = "all";
    let takes = null;
    let loading = false;
    let version = -1;

    const sub = h("div.sub");
    const chips = h("div.chips");
    const jobsBox = h("div.card.hidden", { style: { marginBottom: "18px" } });
    const grid = h("div.takes");
    const empty = h("div.empty.hidden");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Gallery"), sub), h("div.actions", null, chips)),
      jobsBox, grid, empty);

    const renderChips = () => clear(chips, KINDS.map(([k, label]) =>
      h("button.chip", { class: k === filter ? "active" : "", onclick: () => { filter = k; renderChips(); renderTakes(); } }, label)));

    function renderTakes() {
      if (!takes) {
        clear(grid, [0, 1, 2].map(() => h("div.card.take", { style: { opacity: ".5" } }, h("div.thumb"),
                                                  h("div.take-body", null, h("div.take-title", null, " ")))));
        return;
      }
      const shown = filter === "all" ? takes : takes.filter((t) => t.items.some((i) => i.kind === filter));
      empty.classList.toggle("hidden", shown.length > 0);
      clear(empty, icon("film"), h("div", null, takes.length ? "Nothing in this format." : "No recordings yet."),
            !takes.length ? h("div.hint", null, "Recordings appear here as soon as they start.") : null);
      clear(grid, shown.map((t, n) => takeCard(t, n)));
    }

    function takeCard(t, n) {
      const readable = t.items.some((i) => !String(i.problem || "").startsWith("can't"));
      const img = readable ? h("img", { alt: "", loading: "lazy", decoding: "async", src: thumbUrl(t) }) : null;
      const ph = h("div.ph", null, icon(readable ? "film" : "alert"));
      if (img) {
        img.onload = () => { img.classList.add("loaded"); ph.remove(); };
        img.onerror = () => img.remove();
      }
      return h("div.card.take", { style: { animationDelay: Math.min(n, 12) * 30 + "ms" }, onclick: () => openTake(t.id),
                                  role: "button", tabindex: "0", onkeydown: (e) => e.key === "Enter" && openTake(t.id) },
        h("div.thumb", null, ph, img,
          t.recording ? h("span.flag.tag.rec.live", null, h("span.dot"), "REC") : null,
          t.duration ? h("span.dur", null, fmtDur(t.duration)) : null),
        h("div.take-body", null,
          h("div.take-title.ellipsis", null, titleParts(t)[1] || t.title),
          h("div.take-meta.ellipsis", null, [titleParts(t)[1] ? titleParts(t)[0] : null,
            `${t.items.length} file${t.items.length === 1 ? "" : "s"}`, fmtBytes(t.size)].filter(Boolean).join(" · ")),
          h("div.tags", null, t.kinds.map(kindTag))));
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
  const stateTag = { queued: h("span.tag", null, "Queued"), running: h("span.tag.accent", null, pct + "%"),
                     done: h("span.tag.ok", null, "Done"), failed: h("span.tag.rec", null, "Failed"),
                     cancelled: h("span.tag", null, "Cancelled") }[j.state];
  const detail = j.state === "running"
    ? [j.fps ? `${Math.round(j.fps)} fps` : null, j.eta !== null && j.eta !== undefined ? `${fmtDur(j.eta)} left` : null,
       j.live ? "following the recording" : null].filter(Boolean).join(" · ")
    : j.error || "";
  return h("div.job", null,
    h("div.top", null,
      h("div.grow", null,
        h("div.ellipsis", { style: { fontWeight: 550, fontSize: "14px" } }, j.output),
        h("div.muted.ellipsis", { style: { fontSize: "12.5px" } }, `${j.title} · from ${j.source}`)),
      stateTag,
      !finished ? h("button.btn.ghost.icon.sm", { title: "Cancel", "aria-label": "Cancel conversion",
                                                  onclick: (e) => run("jobs.cancel", { id: j.id }, { btn: e.currentTarget }) }, icon("x")) : null),
    !finished ? h("div.bar", null, h("i", { style: { width: pct + "%" } })) : null,
    detail ? h("div", { style: { fontSize: "12.5px", color: j.state === "failed" ? "var(--err)" : "var(--muted)" } }, detail) : null);
}

// ---------------------------------------------------------------- take
export function openTake(takeId) {
  let take = null;
  let playing = null;
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
    if (item) {
      playing = item.id;
      clear(player, h("video", { src: mediaUrl(item), controls: true, playsInline: true, preload: "metadata",
                                 "data-id": item.id, poster: thumbUrl(take) }));
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
              i.busy && !i.recording ? h("span.tag.accent", null, "Converting") : null),
            h("div.meta", null, itemMeta(i)),
            i.problem ? h("div.problem", null, i.problem) : null),
          h("div.acts", null,
            canPlay ? h("button.btn.ghost.icon.sm", { title: "Play", "aria-label": "Play",
                                                      onclick: () => { playing = i.id; player.replaceChildren(); renderPlayer(); } }, icon("play")) : null,
            h("a.btn.ghost.icon.sm", { href: mediaUrl(i, true), download: i.id, title: "Download", "aria-label": "Download" }, icon("download")),
            h("button.btn.ghost.icon.sm", { title: "Convert", "aria-label": "Convert", disabled: i.recording,
                                            onclick: () => openConvert(i) }, icon("convert")),
            h("button.btn.ghost.icon.sm.danger", { title: locked ? "In use" : "Delete this file", "aria-label": "Delete",
                                                   disabled: locked, onclick: () => deleteItem(i) }, icon("trash"))));
      }));
    footDel.disabled = take.items.some((i) => i.recording || i.busy);
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
  s.close = () => { off(); player.querySelector("video")?.pause(); origClose(); };
  refresh();
  return s;
}

// -------------------------------------------------------------- convert
export async function openConvert(item, preselect) {
  let list;
  try { list = await targets(); } catch (e) { toast(errText(e), "err"); return; }
  const caps = new Set(list.filter((t) => t.available).map((t) => t.id));
  let sel = preselect && caps.has(preselect) ? preselect : (list.find((t) => t.available && !sameKind(t.id, item)) || {}).id;
  const opts = { quality: "high", scale: "source", preset: "fast", bitrate: "" };
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
          h("input.input", { type: "number", min: "1", max: "400", placeholder: "auto (quality)", value: opts.bitrate,
                             oninput: (e) => { opts.bitrate = e.target.value; } })) : null) : null);
    startBtn.disabled = !t || !t.available;
  }

  async function start() {
    const args = { file: item.id, target: sel, quality: opts.quality, scale: opts.scale, preset: opts.preset };
    if (opts.bitrate) args.bitrate = Number(opts.bitrate);
    const job = await busy(startBtn, () => conn.call("gallery.convert", args));
    if (job) { toast("Conversion started: " + job.output, "ok"); s.close(); }
  }
  render();
}
