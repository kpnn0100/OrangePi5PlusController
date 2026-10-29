// Internet access (RLY-01, RLY-02, RLY-05, RLY-08): this machine advertises itself to a device
// manager and viewers reach its ports end-to-end encrypted with the access key. Everything
// follows the "relay" topic; the key is fetched only when "Show" or "Copy" is pressed and the
// join token is write-only (the server never sends it back).

import { h, clear, icon, store, conn, run, confirmBox, toggle, copyText, toast, errText, fmtAgo, fmtBytes } from "../core.js";

const STATE = {
  online: ["ok", "Online"],
  connecting: ["accent", "Connecting…"],
  error: ["warn", "Problem"],
  off: ["", "Off"],
};

function portsText(ports) {
  return ports === null || ports === undefined ? "" : ports.join(", ");
}

export default {
  id: "relay", title: "Internet access", icon: "globe",

  mount(root) {
    let st = store.get("relay") || null;
    let key = null;            // the access key text, only after Show / Copy
    let dirty = false;         // the settings form has edits that are not saved

    const statusCard = h("div.card");
    const setCard = h("div.card");
    const keyCard = h("div.card");
    const viewCard = h("div.card");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Internet access"),
        h("div.sub", null, "Reach this machine from anywhere through a device manager"))),
      h("div.grid.two", null, statusCard, setCard, keyCard, viewCard));

    // ------------------------------------------------------------- settings form
    const mark = () => { dirty = true; };
    const url = h("input.input", { type: "url", placeholder: "https://example.com/arstro/internal/devices", spellcheck: "false",
                                   autocomplete: "off", "aria-label": "Device manager URL", oninput: mark });
    const token = h("input.input", { type: "password", spellcheck: "false", autocomplete: "new-password",
                                     "aria-label": "Join token", oninput: mark });
    const name = h("input.input", { type: "text", spellcheck: "false", autocomplete: "off", maxlength: "64",
                                    "aria-label": "Name", oninput: mark });
    const ports = h("input.input", { type: "text", spellcheck: "false", autocomplete: "off", placeholder: "any port",
                                     inputmode: "numeric", "aria-label": "Allowed ports", oninput: mark });
    const onSwitch = toggle(false, (on) => setEnabled(on), { label: "Internet access" });

    function fillForm(force) {
      if (!st || (dirty && !force)) return;
      url.value = st.url || "";
      name.value = st.name || "";
      ports.value = portsText(st.ports);
      token.value = "";
      dirty = false;
    }

    function formParams() {
      const p = { url: url.value.trim() || null, name: name.value.trim() || null, ports: ports.value.trim() || null };
      if (token.value.trim()) p.token = token.value.trim();
      return p;
    }

    async function save(btn) {
      const r = await run("relay.set", formParams(), { btn, ok: "Saved" });
      if (r) { st = r; dirty = false; fillForm(true); renderAll(); }
    }

    async function setEnabled(on) {
      if (!on) {
        const ok = await confirmBox({ title: "Switch Internet access off?", ok: "Switch off", danger: true,
          text: "The machine leaves the device manager's list and every viewer is disconnected." });
        if (!ok) { onSwitch.set(true); return; }
      }
      const params = on ? { ...formParams(), enabled: true } : { enabled: false };
      try {
        st = await conn.call("relay.set", params);
        if (on) { dirty = false; fillForm(true); }
        toast(on ? "Internet access on" : "Internet access off", "ok");
      } catch (e) {
        toast(errText(e), "err");
      }
      renderAll();
    }

    function renderSettings() {
      const s = st || {};
      token.placeholder = s.token_set ? "set - type a new one to replace it" : "the device manager's join token";
      name.placeholder = s.name || "this machine's host name";
      onSwitch.set(!!s.enabled);
      clear(setCard,
        h("div.card-title", null, icon("sliders"), "Settings"),
        h("form.stack", { onsubmit: (e) => { e.preventDefault(); save(e.submitter || null); } },
          h("div.row", null,
            h("div.grow", null, h("b", null, "Internet access"),
              h("div.hint", null, s.enabled ? "On: the machine keeps one outbound connection to the device manager."
                                            : "Off: no connection leaves this machine.")),
            onSwitch),
          h("div.field", null, h("label", null, "Device manager URL"), url),
          h("div.field", null, h("label", null, "Join token"), token,
            h("div.hint", null, s.token_set ? "Saved on the machine; it is never shown again." : "Printed by the device manager when it starts.")),
          h("div.field", null, h("label", null, "Name in the device list"), name),
          h("div.field", null, h("label", null, "Allowed ports"), ports,
            h("div.hint", null, "e.g. 8080, 22. Empty = any port of this machine.")),
          h("div.row.wrap", null,
            h("button.btn.sm.primary", { type: "submit" }, "Save"),
            h("button.btn.sm.ghost", { type: "button", onclick: () => fillForm(true) }, "Undo changes"))));
    }

    // ------------------------------------------------------------------- status
    function renderStatus() {
      if (!st) return clear(statusCard, h("div.empty", null, h("span.spin", { style: { margin: "0 auto" } })));
      const [cls, label] = STATE[st.state] || ["", st.state];
      clear(statusCard,
        h("div.card-title", null, icon("globe"), "Status",
          h("span.tag", { class: cls + (st.state === "online" ? " live" : "") }, st.state === "online" ? h("span.dot") : null, label)),
        st.error ? h("div.io-note", null, icon("alert"), st.error) : null,
        h("dl.kv", { style: { margin: 0 } },
          h("dt", null, "Device manager"), h("dd.mono", { style: { wordBreak: "break-all", fontSize: "13px" } }, st.url || "not set"),
          h("dt", null, "Device id"), h("dd.mono", { style: { wordBreak: "break-all", fontSize: "13px" } }, st.id || "–"),
          h("dt", null, "Name"), h("dd", null, st.name || "–"),
          h("dt", null, "Since"), h("dd", null, st.since ? fmtAgo(st.since) : "–"),
          st.relay_version ? [h("dt", null, "Manager version"), h("dd", null, st.relay_version)] : null,
          st.transport ? [h("dt", null, "Connection"), h("dd", null, st.transport === "http"
            ? "HTTP (the device manager's proxy does not pass WebSockets)" : "WebSocket")] : null,
          h("dt", null, "Ports"), h("dd", null, st.ports ? st.ports.join(", ") : "any")),
        h("div.hint", { style: { marginTop: "10px" } },
          "The machine connects out, so it needs no open port. Viewers pick it in the device manager's list and need its access key."));
    }

    // -------------------------------------------------------------------- key
    async function fetchKey() {
      if (key) return key;
      const r = await conn.call("relay.key");
      key = r.key;
      return key;
    }

    function renderKey() {
      const s = st || {};
      clear(keyCard,
        h("div.card-title", null, icon("key"), "Access key", s.key_id ? h("span.tag.mono", null, s.key_id) : null),
        h("div.stack", null,
          h("div.hint", null, "Give it only to people who may use this machine: with it they reach every allowed port "
            + "(this web UI still asks for its password). The device manager never sees it."),
          h("div.row", null,
            h("span.mono.grow", { style: { fontSize: "13px", wordBreak: "break-all", minWidth: 0 } }, key || "•".repeat(16)),
            h("button.btn.ghost.icon.sm", { "aria-label": key ? "Hide key" : "Show key", onclick: async () => {
              if (key) { key = null; return renderKey(); }
              try { await fetchKey(); } catch (e) { toast(errText(e), "err"); }
              renderKey();
            } }, icon(key ? "hidden" : "eye")),
            h("button.btn.ghost.icon.sm", { "aria-label": "Copy key", onclick: async () => {
              try { copyText(await fetchKey()); } catch (e) { toast(errText(e), "err"); }
            } }, icon("copy"))),
          h("div.row.wrap", null,
            h("button.btn.sm.ghost.danger", { onclick: async (e) => {
              const ok = await confirmBox({ title: "Replace the access key?", ok: "Replace", danger: true,
                text: "Every viewer is disconnected and needs the new key; the old one stops working at once." });
              if (!ok) return;
              const r = await run("relay.rotate", {}, { btn: e.currentTarget, ok: "New access key made" });
              if (r) { key = r.key; renderKey(); }
            } }, icon("refresh"), "Replace key"))));
    }

    // ---------------------------------------------------------------- viewers
    function renderViewers() {
      const s = st || {};
      const list = s.viewers || [];
      clear(viewCard,
        h("div.card-title", null, icon("users"), "Viewers", h("span.tag", null, String(list.length))),
        list.length ? h("div.list", null, list.map((v) => h("div.list-item", null,
          h("div.ico", null, icon("globe")),
          h("div.grow", { style: { minWidth: 0 } },
            h("div.title.ellipsis", null, v.addr || "?"),
            h("div.meta", null, [`since ${fmtAgo(v.since)}`, `${v.streams} open`,
                                 `in ${fmtBytes(v.bytes_in)}`, `out ${fmtBytes(v.bytes_out)}`].join(" · "))))))
          : h("div.muted", { style: { fontSize: "13.5px" } }, "Nobody is connected through the device manager."),
        s.wrong_keys ? h("div.hint", { style: { marginTop: "8px" } },
          `${s.wrong_keys} attempt${s.wrong_keys === 1 ? "" : "s"} with a wrong key since the server started (see Logs).`) : null,
        h("div.row.wrap", { style: { marginTop: "12px" } },
          h("button.btn.sm", { disabled: !list.length, onclick: async (e) => {
            const ok = await confirmBox({ title: "Disconnect every viewer?", ok: "Disconnect", danger: true,
              text: "They can connect again with the access key - replace the key to keep them out." });
            if (ok) run("relay.kick", {}, { btn: e.currentTarget, ok: "Viewers disconnected" });
          } }, icon("x"), "Disconnect all")));
    }

    function renderAll() {
      renderStatus();
      renderSettings();
      renderKey();
      renderViewers();
    }

    let lastKeyId = st && st.key_id;
    const offs = [
      store.on("relay", (s) => {
        if (s && lastKeyId && s.key_id !== lastKeyId) key = null;    // replaced elsewhere
        lastKeyId = s && s.key_id;
        const formChanged = !st || !s || st.enabled !== s.enabled || st.token_set !== s.token_set;
        st = s;
        fillForm(false);
        renderStatus();
        renderViewers();
        renderKey();
        if (formChanged) renderSettings();   // otherwise leave the form alone (focus, typing)
      }),
    ];
    fillForm(true);
    renderAll();
    if (!st && conn.status === "open") conn.call("relay.status").then((s) => { st = s; fillForm(true); renderAll(); }).catch(() => {});
    return () => { offs.forEach((f) => f()); key = null; };
  },
};
