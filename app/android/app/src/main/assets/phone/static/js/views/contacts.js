// Contacts and call log (PHN-09): search the phone's contacts; the recent calls.

import { h, clear, icon, op, errText, seg, debounce, fmtWhen, fmtDur, initials, copyText, featureReady, pref } from "../core.js";

const CALL_TYPES = {
  in: ["in", "Incoming", ""], out: ["out", "Outgoing", ""], missed: ["missed", "Missed", "rec"], rejected: ["ban", "Rejected", "warn"],
};

export default {
  mount(root) {
    let tab = pref.get("contacts.tab", "contacts");
    const body = h("div");
    const tabs = seg([["contacts", "Contacts"], ["calls", "Recent calls"]], tab, (t) => { tab = t; pref.set("contacts.tab", t); show(); }, { label: "Show" });
    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Contacts"), h("div.sub", null, "The phone's contacts and recent calls")),
                  h("div.actions", null, tabs)), body);
    const canMessage = featureReady("messages");
    const msgBtn = (number) => canMessage
      ? h("a.btn.ghost.icon.sm", { href: "#/messages?to=" + encodeURIComponent(number), "aria-label": "Message " + number, title: "Send a message" }, icon("message"))
      : null;

    // ---------------------------------------------------------- contacts
    function contacts() {
      const input = h("input.input", { type: "search", placeholder: "Search by name or number", "aria-label": "Search contacts",
                                       value: pref.get("contacts.q", "") });
      const list = h("div.list");
      const count = h("span.muted", { style: { fontSize: "13px" } });
      clear(body, h("div.card", null, h("div.row", { style: { marginBottom: "10px" } }, h("div.search.grow", null, icon("search"), input), count), list));
      let seq = 0;
      const load = async () => {
        const my = ++seq;
        const qv = input.value.trim();
        pref.set("contacts.q", qv);
        clear(list, h("div.empty.loading", null, h("span.spin")));
        let r;
        try { r = (await op("contacts.list", { q: qv || undefined, limit: 300 })) || []; } catch (e) {
          if (my === seq) clear(list, h("div.empty", null, icon("alert"), h("div", null, errText(e))));
          return;
        }
        if (my !== seq) return;
        count.textContent = r.length ? `${r.length}${r.length >= 300 ? "+" : ""}` : "";
        if (!r.length) { clear(list, h("div.empty", null, icon("users"), h("b", null, qv ? "No contact matches" : "No contacts"))); return; }
        clear(list, r.map((c) => h("div.list-item", { "data-contact": c.name || "" },
          h("div.avatar", null, initials(c.name) === "#" ? icon("user") : initials(c.name)),
          h("div.main-txt", null, h("div.title", null, c.name || (c.numbers || [])[0] || "No name"),
            (c.numbers || []).map((n) => h("div.row", { style: { gap: "4px" } },
              h("span.meta.mono", null, n), msgBtn(n),
              h("button.btn.ghost.icon.sm", { "aria-label": "Copy " + n, title: "Copy", onclick: () => copyText(n) }, icon("copy")))),
            (c.emails || []).map((m) => h("div.meta", null, m))))));
      };
      input.addEventListener("input", debounce(load, 300));
      load();
    }

    // -------------------------------------------------------------- calls
    async function calls() {
      const list = h("div.list");
      clear(body, h("div.card", null, list));
      clear(list, h("div.empty.loading", null, h("span.spin")));
      let r;
      try { r = (await op("contacts.calls", { limit: 200 })) || []; } catch (e) {
        clear(list, h("div.empty", null, icon("alert"), h("div", null, errText(e))));
        return;
      }
      if (!r.length) { clear(list, h("div.empty", null, icon("call"), h("b", null, "No recent calls"))); return; }
      clear(list, r.map((c) => {
        const [ic, label, cls] = CALL_TYPES[c.type] || ["call", c.type || "Call", ""];
        return h("div.list-item", { "data-call": c.number || "" },
          h("div.ico", { class: cls ? "tag " + cls : "", title: label, style: cls ? { height: "38px", padding: 0, justifyContent: "center" } : null }, icon(ic)),
          h("div.main-txt", null, h("div.title", null, c.name || c.number || "Unknown number"),
            h("div.meta", null, [label, c.name ? c.number : null, c.duration_s ? fmtDur(c.duration_s) : null].filter(Boolean).join(" · "))),
          h("div.side-meta", null, fmtWhen(c.date)),
          c.number ? msgBtn(c.number) : null);
      }));
    }

    function show() { tab === "calls" ? calls() : contacts(); }
    show();
    return null;
  },
};
