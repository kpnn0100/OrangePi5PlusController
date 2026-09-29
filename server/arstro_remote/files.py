"""Files module (FILE-01..04): browse, upload, download, rename, delete - inside the
configured roots only (`files_roots` in config.json, default the user's home).

Transfers go over HTTP (web/server.py): GET /api/files/download?path=..., PUT
/api/files/upload?dir=...&name=... (raw body, streamed to disk). Everything else is ops.
"""

import logging
import os
import shutil
import stat
import time

from .session import OpError

log = logging.getLogger("arstro.files")

TEXT_MAX = 512 * 1024


class FilesService:
    def __init__(self, ctx):
        self.ctx = ctx

    def start(self):
        log.info("files: roots %s", ", ".join(self.roots()))

    def shutdown(self):
        pass

    # ------------------------------------------------------------------ paths
    def roots(self):
        out = []
        for r in self.ctx.config.get("files_roots") or ["~"]:
            p = os.path.realpath(os.path.expanduser(str(r)))
            if os.path.isdir(p) and p not in out:
                out.append(p)
        return out

    def resolve(self, path, must_exist=True):
        """Absolute real path inside a root, or OpError."""
        if not path:
            return self.roots()[0]
        p = os.path.realpath(os.path.expanduser(str(path)))
        for r in self.roots():
            if p == r or p.startswith(r.rstrip("/") + "/"):
                if must_exist and not os.path.lexists(p):
                    raise OpError("%s does not exist" % path)
                return p
        raise OpError("%s is outside the shared folders (%s)" % (path, ", ".join(self.roots())))

    def _entry(self, full, name=None):
        st = os.lstat(full)
        kind = ("dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode)
                else "link" if stat.S_ISLNK(st.st_mode) else "other")
        d = {"name": name or os.path.basename(full), "path": full, "type": kind, "size": st.st_size,
             "mtime": int(st.st_mtime), "mode": stat.filemode(st.st_mode)}
        if kind == "link":
            try:
                d["target"] = os.readlink(full)
                d["target_type"] = "dir" if os.path.isdir(full) else "file"
            except OSError:
                pass
        return d

    # --------------------------------------------------------------------- ops
    def handle(self, session, op, msg):
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            raise OpError("unknown op %s" % op)
        return fn(session, msg)

    def op_files_roots(self, _s, _m):
        out = []
        for r in self.roots():
            try:
                du = shutil.disk_usage(r)
                out.append({"path": r, "free": du.free, "total": du.total})
            except OSError:
                out.append({"path": r})
        return {"roots": out}

    def op_files_list(self, _s, msg):
        p = self.resolve(msg.get("path"))
        if not os.path.isdir(p):
            raise OpError("%s is not a folder" % p)
        entries = []
        hidden = bool(msg.get("hidden"))
        try:
            names = os.listdir(p)
        except OSError as e:
            raise OpError("cannot read %s: %s" % (p, e.strerror))
        for name in names:
            if name.startswith(".") and not hidden:
                continue
            try:
                entries.append(self._entry(os.path.join(p, name), name))
            except OSError:
                continue
        entries.sort(key=lambda e: (e["type"] != "dir" and e.get("target_type") != "dir", e["name"].lower()))
        roots = self.roots()
        parent = os.path.dirname(p) if p not in roots else None
        return {"path": p, "parent": parent, "root": next((r for r in roots if p.startswith(r)), None),
                "entries": entries, "writable": os.access(p, os.W_OK)}

    def op_files_upload_check(self, _s, msg):
        """Would an upload of `name` into `dir` be accepted? (Checked before sending the body.)"""
        target = self.upload_target(msg.get("dir"), msg.get("name"), bool(msg.get("overwrite")))
        return {"path": target, "exists": os.path.exists(target)}

    def op_files_stat(self, _s, msg):
        return self._entry(self.resolve(msg.get("path")))

    def op_files_mkdir(self, session, msg):
        parent = self.resolve(msg.get("dir") or os.path.dirname(str(msg.get("path") or "")))
        name = msg.get("name") or os.path.basename(str(msg.get("path") or ""))
        full = os.path.join(parent, self.safe_name(name))
        self.resolve(full, must_exist=False)
        try:
            os.mkdir(full)
        except OSError as e:
            raise OpError("cannot create %s: %s" % (full, e.strerror))
        log.info("folder %s created by session %d (%s)", full, session.num, session.controller)
        return self._entry(full)

    def op_files_rename(self, session, msg):
        src = self.resolve(msg.get("path"))
        if src in self.roots():
            raise OpError("a shared folder itself cannot be renamed")
        to = msg.get("to")
        dst = self.resolve(to if "/" in str(to) else os.path.join(os.path.dirname(src), self.safe_name(to)),
                           must_exist=False)
        if os.path.lexists(dst):
            raise OpError("%s already exists" % dst)
        os.rename(src, dst)
        log.info("%s renamed to %s by session %d (%s)", src, dst, session.num, session.controller)
        return self._entry(dst)

    def op_files_delete(self, session, msg):
        p = self.resolve(msg.get("path"))
        if p in self.roots():
            raise OpError("a shared folder itself cannot be deleted")
        if os.path.isdir(p) and not os.path.islink(p):
            if msg.get("recursive"):
                shutil.rmtree(p)
            else:
                try:
                    os.rmdir(p)
                except OSError:
                    raise OpError("%s is not empty (delete with recursive=true)" % p)
        else:
            os.unlink(p)
        log.info("%s deleted by session %d (%s)", p, session.num, session.controller)
        return {"deleted": p}

    def op_files_read(self, _s, msg):
        """Small text files for a quick look (logs, configs)."""
        p = self.resolve(msg.get("path"))
        if not os.path.isfile(p):
            raise OpError("%s is not a file" % p)
        size = os.path.getsize(p)
        limit = min(int(msg.get("max", TEXT_MAX)), TEXT_MAX)
        with open(p, "rb") as f:
            if msg.get("tail") and size > limit:
                f.seek(size - limit)
            data = f.read(limit)
        if b"\0" in data[:4096]:
            raise OpError("%s looks binary - download it instead" % os.path.basename(p))
        return {"path": p, "size": size, "truncated": size > limit, "text": data.decode("utf-8", "replace")}

    def op_files_write(self, session, msg):
        """Save a small text file (edit a config in the browser)."""
        p = self.resolve(msg.get("path"), must_exist=False)
        text = str(msg.get("text", ""))
        if len(text) > TEXT_MAX:
            raise OpError("text too long (max %d)" % TEXT_MAX)
        tmp = p + ".arstro-tmp"
        with open(tmp, "w") as f:
            f.write(text)
        if os.path.exists(p):
            shutil.copymode(p, tmp)
        os.replace(tmp, p)
        log.info("%s written (%d bytes) by session %d (%s)", p, len(text), session.num, session.controller)
        return self._entry(p)

    # ------------------------------------------------------------ HTTP helpers
    @staticmethod
    def safe_name(name):
        name = str(name or "").strip()
        if not name or name in (".", "..") or "/" in name or "\0" in name:
            raise OpError("bad file name %r" % name)
        return name

    def upload_target(self, directory, name, overwrite=False):
        d = self.resolve(directory)
        if not os.path.isdir(d):
            raise OpError("%s is not a folder" % d)
        full = os.path.join(d, self.safe_name(name))
        self.resolve(full, must_exist=False)
        if os.path.exists(full) and not overwrite:
            raise OpError("%s already exists" % full)
        return full

    def store(self, rfile, length, target, session_label):
        """Stream `length` bytes from rfile to target (atomic rename at the end)."""
        free = shutil.disk_usage(os.path.dirname(target)).free
        if length > free - 64 * 1024 * 1024:
            raise OpError("not enough space (%d MB free)" % (free // 1_000_000))
        tmp = "%s.part-%d" % (target, int(time.time() * 1000))
        done = 0
        try:
            with open(tmp, "wb") as f:
                while done < length:
                    chunk = rfile.read(min(1 << 20, length - done))
                    if not chunk:
                        raise OpError("upload interrupted after %d of %d bytes" % (done, length))
                    f.write(chunk)
                    done += len(chunk)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        log.info("uploaded %s (%d bytes) by %s", target, done, session_label)
        return self._entry(target)
