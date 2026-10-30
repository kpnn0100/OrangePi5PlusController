// Messages (PHN-08): SMS threads, a thread's messages, sending, and new messages live (/ws event
// `sms`). Opened with #/messages?to=<number> it starts on that conversation.

import { h, clear, icon, op, run, toast, errText, onEvent, fmtWhen, toMs, initials, sameNumber, formBox, debounce } from "../core.js";

export default {
  mount(root, { setBadge }) {
    setBadge("messages", 0);
    let threads = [];
    let cur = null;                      // {thread?, address, name?}
    let msgs = [];
    let filter = "";

    const list = h("div.pane-scroll", { role: "list", "aria-label": "Conversations" });
    const search = h("input.input", { type: "search", placeholder: "Search", "aria-label": "Search conversations",
      oninput: debounce(() => { filter = search.value.trim().toLowerCase(); renderThreads(); }, 150) });
    const threadsPane = h("div.card.threads-pane", null,
      h("div.pane-head", null, h("div.ttl", null, "Conversations"),
        h("button.btn.sm.primary", { onclick: () => newMessage() }, icon("plus"), "New")),
      h("div", { style: { padding: "10px 12px 4px" } }, h("div.search", null, icon("search"), search)),
      list);

    const tTitle = h("div.ttl");
    const bubbles = h("div.bubbles");
    const scroller = h("div.pane-scroll", { style: { padding: 0 } }, bubbles);
    const text = h("textarea.input", { rows: "1", placeholder: "Text message", "aria-label": "Message text",
      oninput: () => grow(), onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } } });
    const sendBtn = h("button.btn.primary.icon", { "aria-label": "Send", onclick: () => send() }, icon("send"));
    const threadPane = h("div.card.thread-pane", null,
      h("div.pane-head", null,
        h("button.btn.ghost.icon.sm.back-btn", { "aria-label": "Back to conversations", onclick: () => closeThread() }, icon("left")),
        h("div.avatar.t-avatar"), tTitle),
      scroller,
      h("div.composer", null, text, sendBtn));
    const layout = h("div.msg-layout", null, threadsPane, threadPane);
    root.append(layout);
    const composer = threadPane.querySelector(".composer");
    function noThread() {
      tTitle.replaceChildren("");
      clear(threadPane.querySelector(".t-avatar"));
      composer.classList.add("hidden");
      clear(bubbles, h("div.empty", { style: { paddingTop: "80px" } }, icon("message"), h("b", null, "Pick a conversation"), h("div", null, "or start a new one")));
    }
    noThread();

    function grow() { text.style.height = "40px"; text.style.height = Math.min(140, text.scrollHeight + 2) + "px"; }

    // ----------------------------------------------------------- threads
    function renderThreads() {
      const shown = threads.filter((t) => !filter || `${t.name || ""} ${t.address || ""} ${t.snippet || ""}`.toLowerCase().includes(filter));
      if (!threads.length) { clear(list, h("div.empty", null, icon("message"), h("b", null, "No messages"))); return; }
      clear(list, shown.map((t) => h("div.thread-item", {
        role: "listitem", tabindex: "0", "data-address": t.address || "",
        class: [t.unread ? "unread" : "", cur && isCur(t) ? "active" : ""].join(" "),
        onclick: () => openThread(t), onkeydown: (e) => { if (e.key === "Enter") openThread(t); },
      }, h("div.avatar", null, initials(t.name || t.address) === "#" ? icon("user") : initials(t.name || t.address)),
         h("div.main-txt", null,
           h("div.top", null, h("span.name", null, t.name || t.address || "Unknown"), h("span.when", null, fmtWhen(t.date))),
           h("div.snip", null, t.snippet || "")),
         t.unread ? h("span.unread-dot", { title: `${t.unread} unread` }) : null)));
    }
    const isCur = (t) => (cur.thread !== undefined && t.thread !== undefined && t.thread === cur.thread) || sameNumber(t.address, cur.address);

    async function loadThreads() {
      clear(list, h("div.empty.loading", null, h("span.spin")));
      try {
        threads = (await op("messages.threads", { limit: 100 })) || [];
      } catch (e) {
        clear(list, h("div.empty", null, icon("alert"), h("b", null, "Messages did not load"), h("div", null, errText(e)),
          h("button.btn.sm", { onclick: () => loadThreads(), style: { marginTop: "8px" } }, icon("refresh"), "Try again")));
        return;
      }
      threads.sort((a, b) => (toMs(b.date) || 0) - (toMs(a.date) || 0));
      renderThreads();
    }

    // ------------------------------------------------------------ thread
    async function openThread(t) {
      cur = { thread: t.thread, address: t.address, name: t.name };
      layout.classList.add("in-thread");
      composer.classList.remove("hidden");
      tTitle.replaceChildren(t.name || t.address || "Unknown", t.name ? h("small", null, t.address) : null);
      const av = threadPane.querySelector(".t-avatar");
      clear(av, initials(t.name || t.address) === "#" ? icon("user") : initials(t.name || t.address));
      t.unread = 0;
      renderThreads();
      clear(bubbles, h("div.empty.loading", null, h("span.spin")));
      const mine = cur;
      try {
        const args = t.thread !== undefined && t.thread !== null ? { thread: t.thread, limit: 200 } : { address: t.address, limit: 200 };
        const r = (await op("messages.list", args)) || [];
        if (cur !== mine) return;
        msgs = r.sort((a, b) => (toMs(a.date) || 0) - (toMs(b.date) || 0));
      } catch (e) {
        if (cur !== mine) return;
        msgs = [];
        clear(bubbles, h("div.empty", null, icon("alert"), h("div", null, errText(e))));
        return;
      }
      renderMsgs();
      setTimeout(() => text.focus({ preventScroll: true }), 50);
    }

    function closeThread() {
      layout.classList.remove("in-thread");
      cur = null;
      noThread();
      renderThreads();
    }

    function bubble(m) {
      return h("div.bubble", { class: [m.type === "out" ? "out" : "", m.pending ? "pending" : ""].join(" "), "data-id": m.id || "" },
        m.body || "", h("small", null, m.pending ? (m.failed ? "Not sent" : "Sending…") : fmtWhen(m.date)));
    }

    function renderMsgs() {
      if (!msgs.length) { clear(bubbles, h("div.empty", null, icon("message"), h("div", null, "No messages yet. Write the first one."))); return; }
      const out = [];
      let day = "";
      for (const m of msgs) {
        const ms = toMs(m.date);
        const d = ms ? new Date(ms).toDateString() : "";
        if (d && d !== day) { day = d; out.push(h("div.day-sep", null, new Date(ms).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }))); }
        out.push(bubble(m));
      }
      clear(bubbles, out);
      requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
    }

    async function send() {
      const body = text.value.trim();
      if (!body || !cur) return;
      const to = cur.address;
      const m = { body, type: "out", date: Date.now(), pending: true };
      msgs.push(m);
      renderMsgs();
      text.value = "";
      grow();
      try {
        await op("messages.send", { to, text: body });
        m.pending = false;
        m.date = Date.now();
        bumpThread({ address: to, name: cur.name, snippet: body, date: m.date, thread: cur.thread }, false);
      } catch (e) {
        m.failed = true;
        toast(errText(e), "err");
        if (!text.value) { text.value = body; grow(); }
      }
      renderMsgs();
    }

    function bumpThread(p, unread) {
      let t = threads.find((x) => (p.thread !== undefined && x.thread === p.thread) || sameNumber(x.address, p.address));
      if (!t) { t = { address: p.address, name: p.name, thread: p.thread, unread: 0 }; threads.unshift(t); }
      t.snippet = p.snippet;
      t.date = p.date;
      if (p.name && !t.name) t.name = p.name;
      if (unread) t.unread = (t.unread || 0) + 1;
      threads.sort((a, b) => (toMs(b.date) || 0) - (toMs(a.date) || 0));
      renderThreads();
    }

    async function newMessage(prefill = "") {
      const v = await formBox({ title: "New message", ok: "Send", fields: [
        { name: "to", label: "To (phone number)", type: "tel", value: prefill, placeholder: "+1 555 0100", inputmode: "tel" },
        { name: "text", label: "Message", rows: 4, placeholder: "Text message" }] });
      if (!v || !v.to.trim()) return;
      const to = v.to.trim();
      if (v.text.trim()) {
        const r = await run("messages.send", { to, text: v.text.trim() }, { ok: "Sent" });
        if (!r) return;
        bumpThread({ address: to, snippet: v.text.trim(), date: Date.now() }, false);
      }
      const t = threads.find((x) => sameNumber(x.address, to)) || { address: to };
      openThread(t);
    }

    // -------------------------------------------------------------- live
    const off = onEvent("sms", (e) => {
      const m = { address: e.address, name: e.name, body: e.body, date: e.date || Date.now(), type: "in", read: false };
      const open = cur && sameNumber(cur.address, e.address);
      bumpThread({ address: e.address, name: e.name, snippet: e.body, date: m.date, thread: e.thread }, !open);
      if (open) { msgs.push(m); renderMsgs(); }
      else toast(`New message from ${e.name || e.address}`);
    });

    loadThreads().then(() => {
      const to = new URLSearchParams(location.hash.split("?")[1] || "").get("to");
      if (to) {
        const t = threads.find((x) => sameNumber(x.address, to));
        if (t) openThread(t); else newMessage(to);
      }
    });
    return () => off();
  },
};
