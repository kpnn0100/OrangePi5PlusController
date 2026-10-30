// Notifications (PHN-12): the phone's current notifications, live (/ws event `notification`),
// dismiss, and reply where the app allows it.

import { h, clear, icon, op, run, errText, onEvent, fmtAgo, toMs, formBox, debounce, initials } from "../core.js";

export default {
  mount(root, { setBadge }) {
    setBadge("notifications", 0);
    let items = [];
    const list = h("div.list", { role: "list", "aria-label": "Notifications" });
    const count = h("span.tag");
    root.append(h("div.view-head", null,
      h("div", null, h("h1", null, "Notifications"), h("div.sub", null, "What the phone shows now - live")),
      h("div.actions", null, count, h("button.btn.sm", { onclick: () => load() }, icon("refresh"), "Refresh"))),
      h("div.card", null, list));

    function render(fresh = new Set()) {
      count.textContent = items.length + " now";
      if (!items.length) { clear(list, h("div.empty", null, icon("bell"), h("b", null, "No notifications"))); return; }
      clear(list, items.map((n) => h("div.notif", { role: "listitem", "data-key": n.key, class: fresh.has(n.key) ? "new" : "" },
        h("div.avatar", null, initials(n.app || n.package) === "#" ? icon("bell") : initials(n.app || n.package)),
        h("div.main-txt", null,
          h("div.app", null, n.app || n.package || "", n.time ? h("span", null, "· " + fmtAgo(n.time)) : null),
          n.title ? h("div.ttl", null, n.title) : null,
          n.text ? h("div.txt", null, n.text) : null,
          h("div.acts", null,
            n.can_reply ? h("button.btn.sm", { onclick: () => reply(n) }, icon("reply"), "Reply") : null,
            h("button.btn.sm.ghost", { onclick: (e) => dismiss(n, e.currentTarget) }, icon("x"), "Dismiss"))))));
    }

    async function load() {
      if (!items.length) clear(list, h("div.empty.loading", null, h("span.spin")));
      try {
        items = ((await op("notifications.list")) || []).sort((a, b) => (toMs(b.time) || 0) - (toMs(a.time) || 0));
      } catch (e) {
        clear(list, h("div.empty", null, icon("alert"), h("b", null, "Notifications did not load"), h("div", null, errText(e))));
        return;
      }
      render();
    }

    async function dismiss(n, btn) {
      const r = await run("notifications.dismiss", { key: n.key }, { btn });
      if (r) { items = items.filter((x) => x.key !== n.key); render(); }
    }

    async function reply(n) {
      const v = await formBox({ title: "Reply to " + (n.title || n.app || "notification"), ok: "Send",
                                fields: [{ name: "text", label: "Reply", rows: 3, placeholder: "Your reply" }] });
      if (!v || !v.text.trim()) return;
      await run("notifications.reply", { key: n.key, text: v.text.trim() }, { ok: "Reply sent" });
    }

    // live: posted {key, app, title, text} / removed {key, removed: true}
    const refetch = debounce(() => load(), 1500);
    const off = onEvent("notification", (e) => {
      if (!e.key) return;
      if (e.removed) { items = items.filter((x) => x.key !== e.key); render(); return; }
      const old = items.find((x) => x.key === e.key);
      const n = { ...(old || {}), ...e, time: e.time || Date.now() };
      delete n.ev;
      items = [n, ...items.filter((x) => x.key !== e.key)];
      render(new Set([e.key]));
      if (e.can_reply === undefined && !old) refetch();       // the event lacks can_reply / package: ask once
    });
    load();
    return () => { off(); refetch.cancel(); };
  },
};
