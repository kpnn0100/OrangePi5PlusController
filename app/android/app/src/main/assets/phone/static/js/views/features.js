// Features (PHN-02): every feature of the phone, whether it is on, ready, and what it still needs.
// Read-only: the switches are changed only on the phone, in the Arstro Remote app.

import { h, clear, icon, store } from "../core.js";

const ICONS = {
  status: "overview", screen: "screen", camera: "camera", call: "call", mic: "mic", messages: "message", contacts: "users",
  find: "pin", files: "folder", notifications: "bell", clipboard: "clipboard", apps: "apps", tools: "sliders", shell: "terminal",
};

export default {
  mount(root) {
    const list = h("div.list", { role: "list", "aria-label": "Features" });
    const counts = h("span.tag");
    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Features"), h("div.sub", null, "What this phone shares, and what each feature still needs")),
                  h("div.actions", null, counts)),
      h("div.note-box", { style: { marginBottom: "14px" } }, icon("info"),
        h("span", null, "These are read-only here. Change them in the Arstro Remote app on the phone (This phone) - it asks for the permissions a feature needs when you turn it on.")),
      h("div.card", null, list));

    function render(features) {
      const fs = features || [];
      counts.textContent = `${fs.filter((f) => f.on && f.ready).length} of ${fs.length} ready`;
      clear(list, fs.map((f) => {
        const miss = (f.missing || []).map((m) => (typeof m === "string" ? m : m.name)).filter(Boolean);
        const state = !f.on ? h("span.tag", null, "Off") : f.ready ? h("span.tag.ok", null, h("span.dot"), "Ready") : h("span.tag.warn", null, "Needs permission");
        return h("div.feat-row", { role: "listitem", class: f.on && f.ready ? "ready" : "", "data-feature": f.id },
          h("div.ico", null, icon(ICONS[f.id] || "toggles")),
          h("div.main-txt", null, h("b", null, f.title || f.id),
            f.note ? h("small", null, f.note) : null,
            f.on && miss.length ? h("div.miss", null, h("small", { style: { marginTop: 0 } }, "Still needs:"), miss.map((m) => h("span.tag.warn", null, m))) : null,
            !f.on ? h("small", null, "Turned off on the phone") : null),
          state,
          h("label.switch", { title: "Change it on the phone" }, h("input", { type: "checkbox", checked: !!f.on, disabled: true, "aria-label": (f.title || f.id) + " (read-only)" }), h("span")));
      }));
    }
    return store.on("features", render);
  },
};
