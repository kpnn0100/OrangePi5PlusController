// Apps (APP-01..08): native apps that expose a web UI through NTWB. The list follows the
// "apps" topic; opening an app shows its own web UI (served by the host at /apps/<id>/)
// inside the shell, or in its own tab.

import { h, clear, icon, store, conn, run, sheet, confirmBox, toast, errText } from "../core.js";

const STATE_TAG = { running: "ok", starting: "accent", failed: "warn", stopped: "" };

function openId() {
  const q = location.hash.split("?")[1] || "";
  return new URLSearchParams(q).get("open");
}

export default {
  id: "apps", title: "Apps", icon: "apps",

  mount(root) {
    let apps = store.get("apps") || null;
    let showing = null;                 // app id in the frame
    const body = h("div");
    root.append(body);

    function card(a) {
      const running = a.state === "running" || a.state === "starting";
      return h("div.card.app-card", null,
        h("div.row", { style: { alignItems: "flex-start" } },
          a.icon ? h("img.app-icon", { src: `/apps/${encodeURIComponent(a.id)}/icon`, alt: "" })
                 : h("div.app-icon.ph", null, icon("apps")),
          h("div.grow", null,
            h("div.row", null, h("b.ellipsis", null, a.name || a.id),
              h("span.tag", { class: STATE_TAG[a.state] || "" }, a.state + (a.clients ? ` · ${a.clients} open` : ""))),
            h("div.muted", { style: { fontSize: "13px", marginTop: "2px" } },
              [a.version ? "v" + (a.running_version || a.version) : null, a.origin].filter(Boolean).join(" · ")),
            a.description ? h("div.dim", { style: { fontSize: "13.5px", marginTop: "6px" } }, a.description) : null,
            a.problem ? h("div.io-note", { style: { marginTop: "10px", marginBottom: 0 } }, icon("alert"), a.problem) : null,
            a.detail && a.state === "failed" ? h("div.muted", { style: { fontSize: "12.5px", marginTop: "6px" } }, a.detail) : null)),
        h("div.row.wrap", { style: { marginTop: "14px" } },
          h("button.btn.sm.primary", { disabled: !!a.problem, onclick: () => open(a.id) }, icon("play"), "Open"),
          running ? h("button.btn.sm", { onclick: (e) => stop(a, e.currentTarget) }, "Stop")
                  : h("button.btn.sm", { disabled: !!a.problem, onclick: (e) => run("apps.launch", { app: a.id }, { btn: e.currentTarget, ok: a.name + " started" }) }, "Start"),
          h("button.btn.sm.ghost", { onclick: () => showLog(a) }, icon("logs"), "Log"),
          h("a.btn.sm.ghost", { href: `/apps/${encodeURIComponent(a.id)}/`, target: "_blank", rel: "noopener" }, icon("link"), "New tab")));
    }

    function renderList() {
      if (!apps) return clear(body, h("div.empty", null, h("span.spin", { style: { margin: "0 auto" } })));
      clear(body,
        h("div.view-head", null, h("div", null, h("h1", null, "Apps"),
          h("div.sub", null, "Native programs with a web UI, running on this machine (NTWB)")),
          h("div.actions", null, h("button.btn.sm.ghost", { onclick: () => load() }, icon("refresh"), "Rescan"))),
        apps.length ? h("div.grid.two", null, apps.map(card))
          : h("div.card", null, h("div.empty", null, icon("apps"), h("div", null, "No apps installed."),
              h("div.hint", { style: { marginTop: "8px" } },
                "An app installs itself by putting its ntwb.json into ~/.local/share/ntwb/apps/<id>/ - see docs/ntwb/NTWB.md."))));
    }

    function renderFrame(id) {
      const a = (apps || []).find((x) => x.id === id) || { id, name: id, state: "stopped" };
      const frame = h("iframe.app-frame", { src: `/apps/${encodeURIComponent(id)}/`, title: a.name || id,
                                            allow: "fullscreen; clipboard-read; clipboard-write" });
      clear(body,
        h("div.app-bar", null,
          h("button.btn.sm.ghost", { onclick: () => { location.hash = "#/apps"; } }, icon("up"), "Apps"),
          h("b.grow.ellipsis", null, a.name || id),
          h("span.tag", { class: STATE_TAG[a.state] || "" }, a.state),
          h("a.btn.sm.ghost", { href: `/apps/${encodeURIComponent(id)}/`, target: "_blank", rel: "noopener" }, icon("link"), "New tab"),
          h("button.btn.sm.ghost", { onclick: (e) => stop(a, e.currentTarget) }, "Stop")),
        frame);
    }

    function render() {
      const id = openId();
      if (id) {
        if (showing !== id) { showing = id; renderFrame(id); }
        else {                                   // refresh only the bar's state tag
          const a = (apps || []).find((x) => x.id === id);
          const tag = body.querySelector(".app-bar .tag");
          if (a && tag) { tag.textContent = a.state; tag.className = "tag " + (STATE_TAG[a.state] || ""); }
        }
      } else {
        showing = null;
        renderList();
      }
    }

    function open(id) { location.hash = "#/apps?open=" + encodeURIComponent(id); }

    async function stop(a, btn) {
      const ok = await confirmBox({ title: `Stop ${a.name || a.id}?`, ok: "Stop", danger: true,
        text: "The app is asked to save and exit. Everyone who has it open loses it." });
      if (ok) run("apps.stop", { app: a.id }, { btn, ok: "Stopped" });
    }

    async function showLog(a) {
      let r;
      try { r = await conn.call("apps.log", { app: a.id, lines: 300 }); } catch (e) { return toast(errText(e), "err"); }
      sheet({ title: `${a.name || a.id} · log`, wide: true,
              body: h("div.logview", { style: { height: "60vh" } }, r.lines.length ? r.lines.map((l) => h("div", null, l)) : h("div.muted", null, "(empty)")) });
    }

    async function load() {
      try { apps = (await conn.call("apps.list")).apps; } catch (e) { toast(errText(e), "err"); apps = apps || []; }
      render();
    }

    const offs = [store.on("apps", (v) => { apps = v || []; render(); }, { now: false })];
    const onHash = () => render();
    window.addEventListener("hashchange", onHash);
    render();
    const offReady = conn.on("ready", load);
    if (conn.status === "open") load();
    return () => { offs.forEach((f) => f()); offReady(); window.removeEventListener("hashchange", onHash); };
  },
};
