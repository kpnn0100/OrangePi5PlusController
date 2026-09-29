// Files (FILE-01..04): browse the shared folders, upload (button or drag and drop),
// download, rename, delete, new folder, view / edit small text files.

import { h, clear, icon, conn, run, sheet, formBox, confirmBox, toast, errText, fmtBytes, fmtAgo } from "../core.js";

const TEXT_EXT = /\.(txt|log|md|json|ya?ml|conf|cfg|ini|sh|py|js|css|html|xml|csv|env|service|rules|toml|c|h|cpp|dts)$/i;

export default {
  id: "files", title: "Files", icon: "folder",

  mount(root) {
    let path = null;
    let listing = null;
    let hidden = false;
    const crumbs = h("div.crumbs.grow");
    const listCard = h("div.card", { style: { padding: "6px 14px" } });
    const uploads = h("div.stack", { style: { gap: "6px" } });
    const picker = h("input", { type: "file", multiple: true, class: "hidden", onchange: (e) => { upload([...e.target.files]); e.target.value = ""; } });
    const drop = h("div.dropzone", null, "Drop files here to upload them to this folder");
    const hiddenBtn = h("button.btn.sm.ghost", { onclick: () => { hidden = !hidden; hiddenBtn.classList.toggle("on", hidden); load(path); } }, icon("eye"), "Hidden");
    root.append(
      h("div.view-head", null, h("div", null, h("h1", null, "Files"), h("div.sub", null, "Upload and download files on the Pi")),
        h("div.actions", null, hiddenBtn,
          h("button.btn.sm", { onclick: () => mkdir() }, icon("folder"), "New folder"),
          h("button.btn.sm.primary", { onclick: () => picker.click() }, icon("upload"), "Upload"))),
      h("div.row", { style: { marginBottom: "10px" } }, crumbs), picker, listCard,
      h("div", { style: { marginTop: "12px" } }, drop), uploads);

    for (const ev of ["dragenter", "dragover"]) root.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
    for (const ev of ["dragleave", "drop"]) root.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "drop" || e.target === drop) drop.classList.remove("over"); });
    root.addEventListener("drop", (e) => { if (e.dataTransfer && e.dataTransfer.files.length) upload([...e.dataTransfer.files]); });

    function renderCrumbs() {
      if (!listing) return clear(crumbs);
      const rootPath = listing.root || "/";
      const rel = listing.path.slice(rootPath.length).split("/").filter(Boolean);
      const parts = [[rootPath, rootPath === "/" ? "/" : rootPath.split("/").pop() || rootPath]];
      let acc = rootPath;
      for (const p of rel) { acc = acc.replace(/\/$/, "") + "/" + p; parts.push([acc, p]); }
      clear(crumbs, icon("folder", "dim"), parts.map(([p, n], i) => [i ? h("span.sep", null, "/") : null,
        h("button", { onclick: () => load(p), title: p }, n)]));
    }

    function renderList() {
      if (!listing) return clear(listCard, h("div.empty", null, h("span.spin", { style: { margin: "0 auto" } })));
      const items = [];
      if (listing.parent) items.push(h("div.list-item.click", { onclick: () => load(listing.parent) },
        h("div.ico", null, icon("up")), h("div.grow", null, h("div.title", null, ".."))));
      for (const e of listing.entries) {
        const isDir = e.type === "dir" || e.target_type === "dir";
        items.push(h("div.list-item" + (isDir ? ".click" : ""), { onclick: isDir ? () => load(e.path) : null },
          h("div.ico", null, icon(isDir ? "folder" : "file")),
          h("div.grow", null, h("div.title.ellipsis", null, e.name + (e.type === "link" ? " → " + (e.target || "") : "")),
            h("div.meta", null, [isDir ? "folder" : fmtBytes(e.size), fmtAgo(e.mtime), e.mode].join(" · "))),
          !isDir ? h("a.btn.sm.ghost.icon", { href: "/api/files/download?path=" + encodeURIComponent(e.path), download: e.name,
                                              "aria-label": "Download", title: "Download", onclick: (ev) => ev.stopPropagation() }, icon("download")) : null,
          !isDir && (TEXT_EXT.test(e.name) || e.size < 64 * 1024) ? h("button.btn.sm.ghost.icon", { "aria-label": "Open", title: "View / edit",
                                                   onclick: (ev) => { ev.stopPropagation(); view(e); } }, icon("edit")) : null,
          h("button.btn.sm.ghost.icon", { "aria-label": "More", title: "Rename / delete", onclick: (ev) => { ev.stopPropagation(); more(e); } }, icon("sliders"))));
      }
      clear(listCard, items.length ? h("div.list", null, items) : h("div.empty", null, "Empty folder."));
    }

    async function load(p) {
      try {
        listing = await conn.call("files.list", { path: p, hidden });
        path = listing.path;
      } catch (e) {
        toast(errText(e), "err");
        if (!listing) listing = { path: "", entries: [], parent: null };
      }
      renderCrumbs();
      renderList();
    }

    async function mkdir() {
      const v = await formBox({ title: "New folder", ok: "Create", fields: [{ name: "name", label: "Name" }] });
      if (v && v.name && await run("files.mkdir", { dir: path, name: v.name }, { ok: "Folder created" })) load(path);
    }

    async function more(e) {
      const s = sheet({ title: e.name, body: h("div.stack", null,
          h("div.muted.mono", { style: { fontSize: "12.5px", wordBreak: "break-all" } }, e.path),
          h("button.btn", { onclick: async () => {
            s.close();
            const v = await formBox({ title: "Rename", ok: "Rename", fields: [{ name: "to", label: "New name", value: e.name }] });
            if (v && v.to && v.to !== e.name && await run("files.rename", { path: e.path, to: v.to }, { ok: "Renamed" })) load(path);
          } }, "Rename"),
          h("button.btn.danger", { onclick: async () => {
            s.close();
            const dir = e.type === "dir";
            const ok = await confirmBox({ title: `Delete ${e.name}?`, ok: "Delete", danger: true,
                                          text: dir ? "The folder and everything in it is deleted from the Pi." : "The file is deleted from the Pi." });
            if (ok && await run("files.delete", { path: e.path, recursive: dir }, { ok: "Deleted" })) load(path);
          } }, "Delete")) });
    }

    async function view(e) {
      let r;
      try { r = await conn.call("files.read", { path: e.path, tail: /\.log$/i.test(e.name) }); } catch (x) { return toast(errText(x), "err"); }
      const ta = h("textarea.textview", { spellcheck: "false" });
      ta.value = r.text;
      const s = sheet({ title: e.name, wide: true, body: h("div.stack", null,
          r.truncated ? h("div.hint", null, `Showing ${fmtBytes(r.text.length)} of ${fmtBytes(r.size)} - download for the whole file.`) : null, ta),
        foot: [h("a.btn.ghost", { href: "/api/files/download?path=" + encodeURIComponent(e.path), download: e.name }, icon("download"), "Download"),
               r.truncated ? null : h("button.btn.primary", { onclick: async (ev) => {
                 if (await run("files.write", { path: e.path, text: ta.value }, { btn: ev.currentTarget, ok: "Saved" })) { s.close(); load(path); }
               } }, "Save")] });
    }

    async function upload(list) {
      const dir = path;
      for (const f of list) {
        let overwrite = false;
        try {
          const chk = await conn.call("files.upload_check", { dir, name: f.name, overwrite: true });
          if (chk.exists) {
            overwrite = await confirmBox({ title: `Replace ${f.name}?`, ok: "Replace", danger: true, text: "A file with that name is already in this folder." });
            if (!overwrite) continue;
          }
        } catch (e) { toast(errText(e), "err"); continue; }
        const bar = h("i");
        const row = h("div", null, h("div.row", null, h("span.grow.ellipsis", { style: { fontSize: "13px" } }, f.name), h("span.muted", { style: { fontSize: "12px" } }, fmtBytes(f.size))),
                      h("div.upbar", null, bar));
        uploads.append(row);
        await new Promise((resolve) => {
          const xhr = new XMLHttpRequest();
          xhr.open("PUT", `/api/files/upload?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(f.name)}&overwrite=${overwrite ? 1 : 0}`);
          xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.style.width = (e.loaded / e.total) * 100 + "%"; };
          xhr.onload = () => {
            let r = {};
            try { r = JSON.parse(xhr.responseText); } catch (e) { /* */ }
            if (r.ok) toast("Uploaded " + f.name, "ok"); else toast(r.error || "Upload failed", "err");
            row.remove();
            resolve();
          };
          xhr.onerror = () => { toast("Upload of " + f.name + " failed", "err"); row.remove(); resolve(); };
          xhr.send(f);
        });
      }
      if (path === dir) load(path);
    }

    renderList();
    const start = () => load(path);
    const offReady = conn.on("ready", start);
    if (conn.status === "open") start();
    return () => offReady();
  },
};
