// Shell (PHN-16): run a command in the app's own sandbox (no root) and see its output.

import { h, icon, op, errText, busy, pref } from "../core.js";

export default {
  mount(root) {
    const hist = pref.get("shell.history", []);
    let hi = hist.length;
    const out = h("pre.term", { "aria-live": "polite", "aria-label": "Output" });
    const input = h("input.input", { type: "text", placeholder: "Command, e.g. ls -la", "aria-label": "Command", autocomplete: "off", spellcheck: "false",
                                     autocapitalize: "off" });
    const timeout = h("select.input", { "aria-label": "Time limit", style: { width: "auto" } },
      [[10, "10 s"], [30, "30 s"], [120, "2 min"], [600, "10 min"]].map(([v, l]) => h("option", { value: String(v), selected: v === 30 }, l)));
    const runBtn = h("button.btn.primary", { onclick: () => exec() }, icon("play"), "Run");
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); exec(); }
      else if (e.key === "ArrowUp" && hi > 0) { e.preventDefault(); input.value = hist[--hi]; }
      else if (e.key === "ArrowDown") { e.preventDefault(); hi = Math.min(hist.length, hi + 1); input.value = hist[hi] || ""; }
    });
    out.append(h("span.code", null, "Commands run as the Arstro Remote app, in its own folder - no root.\n"));

    async function exec() {
      const cmd = input.value.trim();
      if (!cmd) return;
      if (hist[hist.length - 1] !== cmd) { hist.push(cmd); if (hist.length > 50) hist.shift(); pref.set("shell.history", hist); }
      hi = hist.length;
      input.value = "";
      out.append(h("span.cmd", null, "$ " + cmd + "\n"));
      const t = Number(timeout.value);
      await busy(runBtn, async () => {
        try {
          const r = await op("shell.run", { cmd, timeout_s: t }, { timeout: (t + 15) * 1000 });
          const text = (r && r.out) || "";
          out.append(text, text && !text.endsWith("\n") ? "\n" : "",
                     h("span.code", { class: r && r.code ? "bad" : "" }, `[exit ${r ? r.code : "?"}]\n`));
        } catch (e) {
          out.append(h("span.bad", null, errText(e) + "\n"));
        }
        out.scrollTop = out.scrollHeight;
      });
      input.focus();
    }

    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Shell"), h("div.sub", null, "A command line inside the app's sandbox")),
                  h("div.actions", null, h("button.btn.sm", { onclick: () => out.replaceChildren() }, icon("trash"), "Clear"))),
      out, h("div.cmd-row", null, input, timeout, runBtn));
    setTimeout(() => input.focus(), 60);
    return null;
  },
};
