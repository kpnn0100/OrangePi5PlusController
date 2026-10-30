// Clipboard (PHN-13): put text on the phone's clipboard; read it - which Android allows only while
// Arstro Remote is in the foreground on the phone (said so instead of failing silently).

import { h, clear, icon, op, run, errText, copyText, busy, toast } from "../core.js";

export default {
  mount(root) {
    const sendIn = h("textarea.input", { rows: "5", placeholder: "Text to put on the phone's clipboard", "aria-label": "Text for the phone's clipboard" });
    const sendBtn = h("button.btn.primary", { onclick: async (e) => {
      const text = sendIn.value;
      if (!text) { toast("Write or paste some text first", "err"); return; }
      await run("clipboard.set", { text }, { btn: e.currentTarget, ok: "On the phone's clipboard" });
    } }, icon("send"), "Send to the phone");
    const pasteBtn = navigator.clipboard && navigator.clipboard.readText
      ? h("button.btn", { onclick: async () => { try { sendIn.value = await navigator.clipboard.readText(); } catch (e) { toast("The browser did not allow reading its clipboard", "err"); } } },
          icon("clipboard"), "Paste from this computer")
      : null;

    const got = h("div.stack");
    const readBtn = h("button.btn", { onclick: (e) => read(e.currentTarget) }, icon("download"), "Read the phone's clipboard");

    async function read(btn) {
      await busy(btn, async () => {
        let r;
        try {
          r = await op("clipboard.get");
        } catch (e) {
          clear(got, h("div.note-box.warn", null, icon("info"), h("span", null, errText(e))));
          return;
        }
        const text = (r && r.text) || "";
        clear(got, text
          ? [h("textarea.input", { rows: "5", readonly: true, "aria-label": "The phone's clipboard" }, text),
             h("div.row", null, h("button.btn.sm", { onclick: () => copyText(text) }, icon("copy"), "Copy here"))]
          : h("div.note-box", null, icon("info"), h("span", null, "The phone's clipboard is empty.")));
      });
    }

    root.append(h("div.view-head", null, h("div", null, h("h1", null, "Clipboard"), h("div.sub", null, "Move text between this browser and the phone"))),
      h("div.grid.two", null,
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("send"), "To the phone"), sendIn,
          h("div.row.wrap", null, sendBtn, pasteBtn),
          h("div.hint", null, "Then long-press any text field on the phone and choose Paste.")),
        h("div.card.stack", null, h("div.card-title", { style: { margin: 0 } }, icon("clipboard"), "From the phone"),
          h("div.note-box", null, icon("info"), h("span", null,
            "Android lets an app read the clipboard only while that app is open on the screen. Open Arstro Remote on the phone first (or use the Screen page), then read.")),
          h("div.row", null, readBtn), got)));
    return null;
  },
};
