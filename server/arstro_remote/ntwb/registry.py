"""Installed NTWB apps (NTWB-02, APP-01): manifests found in the XDG data dirs, plus manifests
registered by path (`apps.register`).

An app is installed by putting `<data dir>/ntwb/apps/<id>/ntwb.json` next to (or pointing
at) its adapter and web UI - the same idea as a .desktop file. The host rescans on every
`apps.list`, so installing needs no host restart.
"""

import json
import logging
import os

from . import spec

log = logging.getLogger("arstro.apps")


def search_dirs():
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = [home] + [d for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    return [os.path.join(d, "ntwb", "apps") for d in dirs]


class App:
    """One installed app: its manifest, resolved paths, and whether it is usable."""

    def __init__(self, manifest_path, manifest, origin):
        self.manifest_path = manifest_path
        self.dir = os.path.dirname(os.path.abspath(manifest_path))
        self.m = manifest
        self.origin = origin          # "installed" | "registered"
        self.problem = None
        self.api = None
        self.valid = False            # the manifest itself conforms (the app may still be broken)
        try:
            spec.validate_manifest(manifest)
        except spec.SpecError as e:
            self.problem = str(e)
            return
        self.valid = True
        self.id = manifest["id"]
        if not os.path.isfile(os.path.join(self.web_dir, "index.html")):
            self.problem = "web UI missing (%s has no index.html)" % self.web_dir
        exe = self.argv[0]
        if not os.path.isfile(exe) or not os.access(exe, os.X_OK):
            self.problem = self.problem or "cannot execute %s" % exe
        if manifest.get("api"):
            try:
                with open(self._path(manifest["api"])) as f:
                    self.api = json.load(f)
                spec.validate_app_api(self.api)
            except (OSError, ValueError, spec.SpecError) as e:
                self.problem = self.problem or "bad API description: %s" % e
                self.api = None

    @property
    def id_or_none(self):
        return self.m.get("id") if isinstance(self.m, dict) else None

    def _path(self, p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.dir, p))

    @property
    def web_dir(self):
        return self._path(self.m["web"])

    @property
    def argv(self):
        a = list(self.m["exec"])
        a[0] = self._path(a[0])
        return a

    @property
    def cwd(self):
        return self._path(self.m.get("cwd") or ".")

    @property
    def icon_path(self):
        return self._path(self.m["icon"]) if self.m.get("icon") else None

    def allows(self, method):
        """NTWB-07: with an API description, only its methods may be called."""
        return self.api is None or method in self.api.get("methods", {})

    def describe(self):
        m = self.m if isinstance(self.m, dict) else {}
        return {"id": m.get("id"), "name": m.get("name") or m.get("id"), "version": m.get("version"),
                "description": m.get("description"), "icon": bool(m.get("icon")), "origin": self.origin,
                "manifest": self.manifest_path, "problem": self.problem, "api": self.api is not None,
                "capabilities": m.get("capabilities") or []}


def _load(path, origin):
    try:
        with open(path) as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        a = App(path, {}, origin)
        a.problem = "cannot read manifest: %s" % e
        return a
    return App(path, m, origin)


def scan(registered=()):
    """{id: App} - first definition of an id wins (XDG order, then registered paths).
    Unreadable / invalid manifests are returned under a synthetic key so they show up
    with their problem instead of disappearing."""
    apps = {}
    bad = 0
    candidates = []
    for d in search_dirs():
        try:
            for name in sorted(os.listdir(d)):
                candidates.append((os.path.join(d, name, "ntwb.json"), "installed"))
        except OSError:
            continue
    candidates += [(p, "registered") for p in registered]
    for path, origin in candidates:
        if not os.path.isfile(path):
            if origin == "registered":
                a = App(path, {}, origin)
                a.problem = "manifest not found"
                bad += 1
                apps["!%d" % bad] = a
            continue
        a = _load(path, origin)
        key = a.id if a.valid else "!%s" % path
        if key in apps:
            log.debug("app %s at %s shadowed by an earlier one", key, path)
            continue
        apps[key] = a
    return apps
