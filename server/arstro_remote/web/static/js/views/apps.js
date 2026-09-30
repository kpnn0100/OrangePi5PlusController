// Apps (APP-01..09): native apps that expose a web UI through NTWB. The list follows the
// "apps" topic; opening an app shows its own web UI (served by the host at /apps/<id>/)
// inside the shell, or in its own tab. An app whose manifest says `single: false` runs
// sessions - each its own process and model (APP-04) - listed here with their viewers.

import { h, clear, icon, store, conn, run, sheet, confirmBox, toast, errText } from "../core.js";

const STATE_TAG = { running: "ok", starting: "accent", failed: "warn", stopped: "" };
const MAIN = "main";

function openTarget() {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  return q.get("open") ? { id: q.get("open"), session: q.get("session") || MAIN } : null;
}

function appUrl(id, session) {
  return `/apps/${encodeURIComponent(id)}/` + (session && session !== MAIN ? `?session=${encodeURIComponent(session)}` : "");
}

function viewers(n) { return n === 1 ? "1 viewing" : `${n} viewing`; }

export default {
  id: "apps", title: "Apps", icon: "apps",

  mount(root) {
    let apps = store.get("apps") || null;
    let showing = null;                 // "id/session" in the frame
    const body = h("div");
    root.append(body);

    function sessionRow(a, s) {
      const running = s.state === "running" || s.state === "starting";
      return h("div.row.app-session", null,
        h("span.mono.grow.ellipsis", null, s.session),
        h("span.tag", { class: STATE_TAG[s.state] || "" }, s.state + (s.clients ? ` · ${viewers(s.clients)}` : "")),
        h("button.btn.sm.ghost", { onclick: () => open(a.id, s.session) }, "Open"),
        h("a.btn.sm.ghost", { href: appUrl(a.id, s.session), target: "_blank", rel: "noopener", title: "New tab" }, icon("link")),
        running ? h("button.btn.sm.ghost", { onclick: (e) => stop(a, e.currentTarget, s.session) }, "Stop") : null);
    }

    function card(a) {
      const running = a.state === "running" || a.state === "starting";
      const sessions = a.sessions || [];
      const multi = a.single === false;
      return h("div.card.app-card", null,
        h("div.row", { style: { alignItems: "flex-start" } },
          a.icon ? h("img.app-icon", { src: `/apps/${encodeURIComponent(a.id)}/icon`, alt: "" })
                 : h("div.app-icon.ph", null, icon("apps")),
          h("div.grow", null,
            h("div.row", null, h("b.ellipsis", null, a.name || a.id),
              h("span.tag", { class: STATE_TAG[a.state] || "" }, a.state + (a.clients ? ` · ${viewers(a.clients)}` : ""))),
            h("div.muted", { style: { fontSize: "13px", marginTop: "2px" } },
              [a.version ? "v" + (a.running_version || a.version) : null, a.origin,
               multi ? `${sessions.length} session${sessions.length === 1 ? "" : "s"}` : null].filter(Boolean).join(" · ")),
            a.description ? h("div.dim", { style: { fontSize: "13.5px", marginTop: "6px" } }, a.description) : null,
            a.problem ? h("div.io-note", { style: { marginTop: "10px", marginBottom: 0 } }, icon("alert"), a.problem) : null,
            a.detail && a.state === "failed" ? h("div.muted", { style: { fontSize: "12.5px", marginTop: "6px" } }, a.detail) : null)),
        multi && !a.problem ? h("div.app-sessions", null, sessions.map((s) => sessionRow(a, s))) : null,
        h("div.row.wrap", { style: { marginTop: "14px" } },
          h("button.btn.sm.primary", { disabled: !!a.problem, onclick: () => open(a.id, MAIN) }, icon("play"), "Open"),
          multi ? h("button.btn.sm", { disabled: !!a.problem, onclick: (e) => newSession(a, e.currentTarget) }, "New session") : null,
          running ? h("button.btn.sm", { onclick: (e) => stop(a, e.currentTarget) }, multi && sessions.length > 1 ? "Stop all" : "Stop")
                  : h("button.btn.sm", { disabled: !!a.problem, onclick: (e) => run("apps.launch", { app: a.id }, { btn: e.currentTarget, ok: a.name + " started" }) }, "Start"),
          h("button.btn.sm.ghost", { onclick: () => showLog(a) }, icon("logs"), "Log"),
          h("a.btn.sm.ghost", { href: appUrl(a.id, MAIN), target: "_blank", rel: "noopener" }, icon("link"), "New tab")));
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

    function sessionOf(id, session) {
      const a = (apps || []).find((x) => x.id === id);
      return { a: a || { id, name: id, state: "stopped" },
               s: (a && (a.sessions || []).find((x) => x.session === session)) || { session, state: a ? a.state : "stopped", clients: 0 } };
    }

    function barTag(s) { return s.state + (s.clients ? ` · ${viewers(s.clients)}` : ""); }

    function renderFrame(id, session) {
      const { a, s } = sessionOf(id, session);
      const frame = h("iframe.app-frame", { src: appUrl(id, session), title: a.name || id,
                                            allow: "fullscreen; clipboard-read; clipboard-write" });
      clear(body,
        h("div.app-bar", null,
          h("button.btn.sm.ghost", { onclick: () => { location.hash = "#/apps"; } }, icon("up"), "Apps"),
          h("b.grow.ellipsis", null, a.name || id, session !== MAIN ? h("span.muted.mono", null, "  " + session) : null),
          h("span.tag", { class: STATE_TAG[s.state] || "" }, barTag(s)),
          h("a.btn.sm.ghost", { href: appUrl(id, session), target: "_blank", rel: "noopener" }, icon("link"), "New tab"),
          h("button.btn.sm.ghost", { onclick: (e) => stop(a, e.currentTarget, session) }, "Stop")),
        frame);
    }

    function render() {
      const target = openTarget();
      if (target) {
        const key = target.id + "/" + target.session;
        if (showing !== key) { showing = key; renderFrame(target.id, target.session); }
        else {                                   // refresh only the bar's state tag
          const { s } = sessionOf(target.id, target.session);
          const tag = body.querySelector(".app-bar .tag");
          if (tag) { tag.textContent = barTag(s); tag.className = "tag " + (STATE_TAG[s.state] || ""); }
        }
      } else {
        showing = null;
        renderList();
      }
    }

    function open(id, session) {
      location.hash = "#/apps?open=" + encodeURIComponent(id) + (session && session !== MAIN ? "&session=" + encodeURIComponent(session) : "");
    }

    async function newSession(a, btn) {
      // Start it here rather than via ?session=new in the frame, so the shell's own URL names the
      // real session and a reload of the shell rejoins it instead of starting yet another.
      const r = await run("apps.launch", { app: a.id, session: "new" }, { btn, ok: "New session started" });
      if (r && r.session) open(a.id, r.session);
    }

    async function stop(a, btn, session) {
      const one = session !== undefined;
      const ok = await confirmBox({ title: `Stop ${a.name || a.id}${one && session !== MAIN ? " · " + session : ""}?`, ok: "Stop", danger: true,
        text: one ? "The session is asked to save and exit. Everyone who has it open loses it."
                  : "The app is asked to save and exit - every session of it. Everyone who has it open loses it." });
      if (ok) run("apps.stop", one ? { app: a.id, session } : { app: a.id }, { btn, ok: "Stopped" });
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
