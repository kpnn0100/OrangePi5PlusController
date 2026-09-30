// The example app's web UI: state, events, calls and a blob stream through ntwb.js.
const app = NTWB.connect();
const $ = (id) => document.getElementById(id);
const log = (text) => { const li = document.createElement("li"); li.textContent = text; $("log").prepend(li); };

app.onStatus((s, detail) => { $("status").textContent = s + (detail ? " – " + detail : ""); });
app.onState("counter", (n) => { $("count").textContent = n; });
app.onEvent("added", (d) => log(`${d.by} added ${d.n}`));
app.onBlob("picture", (blob, h) => { $("img").src = URL.createObjectURL(blob); log(`picture ${h.meta.size}px`); });
$("plus").onclick = () => app.call("add", { n: 1 }).catch((e) => log(e.message));
$("minus").onclick = () => app.call("add", { n: -1 }).catch((e) => log(e.message));
$("pic").onclick = () => app.call("picture", { size: 96 }).catch((e) => log(e.message));
