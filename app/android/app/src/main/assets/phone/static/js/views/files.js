// Files (PHN-11): browse the phone's storage, download (plain <a download> links: the device
// manager's shim fetches them through the channel and shows them in its Transfers panel), upload
// any size (XHR PUT with the File as body, so the exact Content-Length goes along), new folder,
// rename, delete. The folder is kept in the address: #/files?path=<dir>.

import { h, clear, icon, getJson, run, toast, errText, fmtBytes, fmtWhen, formBox, confirmBox, q } from "../core.js";

function hashPath() {
  return new URLSearchParams(location.hash.split("?")[1] || "").get("path") || "";
}

export default {
  mount(root) {
    let cur = null;                   // the listing: {path, parent, entries}
    let home = null;                  // the storage root (the first listing without a path)
    let seq = 0;

    const crumbs = h("nav.crumbs", { "aria-label": "Folder" });
    const fileIn = h("input", { type: "file", multiple: true, class: "hidden", onchange: () => { uploadAll([...fileIn.files]); fileIn.value = ""; } });
    const head = h("div.view-head", null,
      h("div", { style: { minWidth: 0, flex: "1" } }, h("h1", null, "Files"), crumbs),
      h("div.actions", null,
        h("button.btn.sm.primary", { onclick: () => fileIn.click() }, icon("upload"), "Upload"),
        h("button.btn.sm", { onclick: () => mkdir() }, icon("plus"), "New folder"),
        h("button.btn.sm.icon", { onclick: () => load(cur && cur.path), "aria-label": "Refresh", title: "Refresh" }, icon("refresh")),
        fileIn));
    const uploads = h("div.upl");
    const list = h("div", { role: "list", "aria-label": "Files" });
    const card = h("div.card.flat", null, list);
    root.append(head, uploads, card);

    // drag and drop onto the list
    card.addEventListener("dragover", (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); card.classList.add("drop-hint"); } });
    card.addEventListener("dragleave", () => card.classList.remove("drop-hint"));
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      card.classList.remove("drop-hint");
      const files = [...(e.dataTransfer?.files || [])];
      if (files.length) uploadAll(files);
    });

    function renderCrumbs() {
      if (!cur) { clear(crumbs); return; }
      const parts = [];
      const path = cur.path || "/";
      if (home && (path === home || path.startsWith(home + "/"))) {
        parts.push(["Storage", home]);
        let acc = home;
        for (const seg of path.slice(home.length).split("/").filter(Boolean)) { acc += "/" + seg; parts.push([seg, acc]); }
      } else {
        let acc = "";
        parts.push(["/", "/"]);
        for (const seg of path.split("/").filter(Boolean)) { acc += "/" + seg; parts.push([seg, acc]); }
      }
      clear(crumbs, parts.map(([label, p], i) => [
        i ? icon("right") : null,
        h("button", { class: i === parts.length - 1 ? "cur" : "", title: p, onclick: () => go(p) }, label)]));
    }

    function go(path) {
      const want = "#/files" + (path ? "?" + q({ path }) : "");
      if (location.hash !== want) location.hash = want;
      else load(path);
    }

    async function load(path) {
      const my = ++seq;
      if (!cur) clear(list, h("div.empty.loading", null, h("span.spin")));
      let r;
      try {
        r = await getJson("api/files/list" + (path ? "?" + q({ path }) : ""));
      } catch (e) {
        if (my !== seq) return;
        clear(list, h("div.empty", null, icon("alert"), h("b", null, "This folder did not open"), h("div", null, errText(e)),
          h("button.btn.sm", { style: { marginTop: "8px" }, onclick: () => go(home || "") }, "Go to storage")));
        return;
      }
      if (my !== seq) return;
      if (!path && !home) home = r.path;
      if (!home && r.parent === null) home = r.path;
      cur = r;
      renderCrumbs();
      render();
    }

    function render() {
      const entries = [...(cur.entries || [])].sort((a, b) => (b.dir - a.dir) || String(a.name).localeCompare(String(b.name), undefined, { numeric: true, sensitivity: "base" }));
      const rows = [];
      if (cur.parent) {
        rows.push(h("div.file-row.dir", { role: "listitem" }, h("div.ico", null, icon("up")),
          h("div.name", { onclick: () => go(cur.parent), tabindex: "0", onkeydown: (e) => { if (e.key === "Enter") go(cur.parent); } },
            h("b", null, ".."), h("small", null, "Up one folder"))));
      }
      for (const e of entries) {
        const open = () => go(e.path);
        rows.push(h("div.file-row", { class: e.dir ? "dir" : "", role: "listitem", "data-name": e.name },
          h("div.ico", null, icon(e.dir ? "folder" : "file")),
          h("div.name", e.dir ? { onclick: open, tabindex: "0", onkeydown: (ev) => { if (ev.key === "Enter") open(); } } : null,
            h("b", { title: e.name }, e.name),
            h("small", null, [e.dir ? "Folder" : fmtBytes(e.size), e.mtime ? fmtWhen(e.mtime) : null].filter(Boolean).join(" · "))),
          h("div.acts", null,
            e.dir ? null : h("a.btn.ghost.icon.sm", { href: "api/files/download?" + q({ path: e.path }), download: e.name,
                                                      "aria-label": "Download " + e.name, title: "Download" }, icon("download")),
            h("button.btn.ghost.icon.sm", { "aria-label": "Rename " + e.name, title: "Rename", onclick: () => rename(e) }, icon("edit")),
            h("button.btn.ghost.icon.sm.danger", { "aria-label": "Delete " + e.name, title: "Delete", onclick: () => remove(e) }, icon("trash")))));
      }
      if (!entries.length) rows.push(h("div.empty", null, icon("folder"), h("b", null, "Empty folder"), h("div", null, "Upload files or drop them here.")));
      clear(list, rows);
    }

    async function mkdir() {
      const v = await formBox({ title: "New folder", ok: "Create", fields: [{ name: "name", label: "Name", placeholder: "New folder" }] });
      if (!v || !v.name.trim()) return;
      const r = await run("files.mkdir", { path: join(cur.path, v.name.trim()) }, { ok: "Folder created" });
      if (r) load(cur.path);
    }

    async function rename(e) {
      const v = await formBox({ title: "Rename", ok: "Rename", fields: [{ name: "name", label: "New name", value: e.name }] });
      if (!v || !v.name.trim() || v.name.trim() === e.name) return;
      const r = await run("files.rename", { path: e.path, name: v.name.trim() }, { ok: "Renamed" });
      if (r) load(cur.path);
    }

    async function remove(e) {
      const ok = await confirmBox({ title: `Delete ${e.dir ? "folder" : "file"}?`, danger: true, ok: "Delete",
        text: `“${e.name}”${e.dir ? " and everything in it" : ""} will be deleted from the phone. This can't be undone.` });
      if (!ok) return;
      const r = await run("files.delete", { path: e.path }, { ok: "Deleted" });
      if (r) load(cur.path);
    }

    // ------------------------------------------------------------ upload
    async function uploadAll(files) {
      const dir = cur && cur.path;
      if (!dir) return;
      for (const f of files) await uploadOne(f, dir);
      if (cur && cur.path === dir) load(dir);
    }

    function uploadOne(file, dir, overwrite = false) {
      return new Promise((resolve) => {
        const bar = h("i", { style: { width: "0%" } });
        const pct = h("span.muted.num", null, "0%");
        const xhr = new XMLHttpRequest();
        const row = h("div.upl-row", null, icon("upload"), h("span.ellipsis", { style: { maxWidth: "40%" } }, file.name),
          h("div.bar", null, bar), pct,
          h("button.btn.ghost.icon.sm", { "aria-label": "Cancel upload", onclick: () => xhr.abort() }, icon("x")));
        uploads.append(row);
        const done = (msg, kind) => { row.remove(); if (msg) toast(msg, kind); resolve(); };
        xhr.open("PUT", "api/files/upload?" + q({ dir, name: file.name, overwrite: overwrite ? 1 : undefined }));
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.upload.onprogress = (e) => {
          if (!e.lengthComputable) return;
          const p = (e.loaded / e.total) * 100;
          bar.style.width = p.toFixed(1) + "%";
          // behind the device manager the browser hands the whole body over at once: the viewer's
          // Transfers panel shows the real progress; here "sending" until the phone answers
          pct.textContent = p >= 100 ? "sending…" : Math.round(p) + "%";
        };
        xhr.onload = async () => {
          let d = null;
          try { d = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
          if (xhr.status >= 200 && xhr.status < 300 && (!d || d.ok !== false)) { done(`Uploaded ${file.name}`, "ok"); return; }
          if (xhr.status === 409 && !overwrite) {
            row.remove();
            const yes = await confirmBox({ title: "Replace the file?", text: `“${file.name}” already exists in this folder. Replace it?`, ok: "Replace", danger: true });
            if (yes) await uploadOne(file, dir, true);
            resolve();
            return;
          }
          done((d && d.error) || `Upload of ${file.name} failed (HTTP ${xhr.status})`, "err");
        };
        xhr.onerror = () => done(`Upload of ${file.name} failed: the phone is not reachable`, "err");
        xhr.onabort = () => done(`Upload of ${file.name} cancelled`);
        xhr.send(file);                // a File body: the browser sends its exact Content-Length
      });
    }

    const onHash = () => { if (location.hash.startsWith("#/files")) { const p = hashPath(); if (!cur || p !== cur.path) load(p); } };
    window.addEventListener("hashchange", onHash);
    load(hashPath());
    return () => window.removeEventListener("hashchange", onHash);
  },
};

function join(dir, name) {
  return (dir.endsWith("/") ? dir : dir + "/") + name;
}
