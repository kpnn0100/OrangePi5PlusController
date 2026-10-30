// Apps (PHN-14): the installed apps with their icons (api/apps/icon, loaded lazily), search, open one.

import { h, clear, icon, op, run, errText, debounce, toggle, pref, q, initials } from "../core.js";

export default {
  mount(root) {
    let apps = [];
    let showSystem = pref.get("apps.system", false);
    const grid = h("div.app-grid", { role: "list", "aria-label": "Apps" });
    const input = h("input.input", { type: "search", placeholder: "Search apps", "aria-label": "Search apps",
                                     oninput: debounce(() => render(), 120) });
    const count = h("span.muted", { style: { fontSize: "13px", whiteSpace: "nowrap" } });
    const sys = toggle(showSystem, (v) => { showSystem = v; pref.set("apps.system", v); render(); }, { label: "Show system apps" });
    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Apps"), h("div.sub", null, "Open an app on the phone"))),
      h("div.card", null,
        h("div.row.wrap", { style: { marginBottom: "14px" } }, h("div.search.grow", { style: { minWidth: "200px" } }, icon("search"), input),
          h("label.row", { style: { gap: "8px", cursor: "pointer" } }, sys, h("span.dim", { style: { fontSize: "14px" } }, "System apps")), count),
        grid));

    function render() {
      const f = input.value.trim().toLowerCase();
      const shown = apps.filter((a) => (showSystem || !a.system) && (!f || `${a.label} ${a.package}`.toLowerCase().includes(f)));
      count.textContent = `${shown.length} of ${apps.length}`;
      if (!shown.length) { clear(grid, h("div.empty", { style: { gridColumn: "1 / -1" } }, icon("apps"), h("b", null, apps.length ? "No app matches" : "No apps"))); return; }
      clear(grid, shown.map((a) => {
        const img = h("img", { src: "api/apps/icon?" + q({ package: a.package }), alt: "", loading: "lazy", decoding: "async", width: "48", height: "48" });
        img.addEventListener("error", () => img.replaceWith(h("span.ph", null, initials(a.label))), { once: true });
        return h("button.app-tile", { role: "listitem", title: `Open ${a.label} (${a.package})`, "data-package": a.package,
                                      onclick: (e) => run("apps.open", { package: a.package }, { btn: e.currentTarget, ok: `Opened ${a.label}` }) },
          img, h("span", null, a.label || a.package));
      }));
    }

    (async () => {
      clear(grid, h("div.empty.loading", { style: { gridColumn: "1 / -1" } }, h("span.spin")));
      try {
        apps = ((await op("apps.list")) || []).sort((a, b) => String(a.label).localeCompare(String(b.label)));
      } catch (e) {
        clear(grid, h("div.empty", { style: { gridColumn: "1 / -1" } }, icon("alert"), h("b", null, "Apps did not load"), h("div", null, errText(e))));
        return;
      }
      render();
    })();
    return null;
  },
};
