// Logs (LOG-01..04): the server's log files, live tail, filter, log level, markers.

import { h, clear, icon, conn, run, seg, formBox, toast, errText } from "../core.js";

export default {
  id: "logs", title: "Logs", icon: "logs",

  mount(root) {
    let file = "arstro-remote";
    let files = [];
    let level = "info";
    let follow = true;
    let timer = null;
    const fileSel = h("select.input", { style: { width: "auto" }, onchange: (e) => { file = e.target.value; tick(true); } });
    const grep = h("input.input", { placeholder: "Filter…", spellcheck: "false", style: { maxWidth: "200px" },
                                    oninput: () => { clearTimeout(grep.t); grep.t = setTimeout(() => tick(true), 300); } });
    const levelBox = h("div");
    const view = h("div.logview");
    const followBtn = h("button.btn.sm", { class: "primary", onclick: () => { follow = !follow; followBtn.classList.toggle("primary", follow); if (follow) tick(true); } }, "Live");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Logs"), h("div.sub", null, "What the server did, for debugging")),
        h("div.actions", null,
          h("button.btn.sm.ghost", { onclick: () => mark() }, icon("plus"), "Mark"),
          h("a.btn.sm.ghost", { href: "#", onclick: (e) => { e.preventDefault(); download(); } }, icon("download"), "Download"))),
      h("div.row", { style: { flexWrap: "wrap", marginBottom: "10px" } }, fileSel, grep, h("div.grow"), levelBox, followBtn),
      view);

    const esc = (l) => {
      const m = l.match(/^\S+ \S+ (DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/);
      return h("div", { class: m ? m[1] : "" }, l);
    };

    async function tick(scrollDown) {
      try {
        const r = await conn.call("log.tail", { file, lines: 800, grep: grep.value || undefined });
        const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 30;
        clear(view, r.lines.length ? r.lines.map(esc) : h("div.muted", null, "(empty)"));
        if (scrollDown || atBottom) view.scrollTop = view.scrollHeight;
      } catch (e) { clear(view, h("div", { class: "ERROR" }, errText(e))); }
    }

    function renderLevel() {
      clear(levelBox, seg([["debug", "Debug"], ["info", "Info"], ["warning", "Warn"]], level, async (v) => {
        try { level = (await conn.call("log.level", { level: v, save: true })).level; toast("Log level " + level, "ok"); }
        catch (e) { toast(errText(e), "err"); }
      }));
    }

    async function mark() {
      const v = await formBox({ title: "Add a marker", ok: "Add", text: "Writes a line into the log, so you can find the moment later.",
                                fields: [{ name: "text", label: "Text", value: "reproducing the problem now" }] });
      if (v && await run("log.mark", { text: v.text })) tick(true);
    }

    async function download() {
      const r = await conn.call("log.tail", { file, lines: 2000 }).catch((e) => toast(errText(e), "err"));
      if (!r) return;
      const url = URL.createObjectURL(new Blob([r.lines.join("\n") + "\n"], { type: "text/plain" }));
      const a = h("a", { href: url, download: `${file}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.log` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    }

    async function load() {
      try {
        const [f, l] = await Promise.all([conn.call("log.files"), conn.call("log.level", {})]);
        files = f.files;
        level = l.level;
      } catch (e) { toast(errText(e), "err"); }
      clear(fileSel, files.map((x) => h("option", { value: x.name, selected: x.name === file }, x.name)));
      renderLevel();
      tick(true);
      clearInterval(timer);
      timer = setInterval(() => { if (follow && !document.hidden) tick(false); }, 2000);
    }
    renderLevel();
    const offReady = conn.on("ready", load);
    if (conn.status === "open") load();
    return () => { offReady(); clearInterval(timer); };
  },
};
