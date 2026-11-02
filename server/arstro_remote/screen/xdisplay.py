"""The X desktop's size and its monitors (SCR-03, SCR-06) through Xlib / XRandR, with ctypes only.

    python3 -m arstro_remote.screen.xdisplay [--display :0]   -> JSON {"screen": [w, h], "monitors": [...]}

Monitors are RandR 1.5's (what `xrandr --listactivemonitors` shows): one per active output, or
the ones a desktop defined itself. Without RandR the whole X screen is the only monitor. An X
error must never end the caller, so the error handlers only note it - still, only helper
processes call this (the capture worker, a short-lived probe): never the daemon itself (ARC-06).
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import sys


class _MonitorInfo(ctypes.Structure):           # XRRMonitorInfo (X11/extensions/Xrandr.h)
    _fields_ = [("name", ctypes.c_ulong), ("primary", ctypes.c_int), ("automatic", ctypes.c_int),
                ("noutput", ctypes.c_int), ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("width", ctypes.c_int), ("height", ctypes.c_int), ("mwidth", ctypes.c_int),
                ("mheight", ctypes.c_int), ("outputs", ctypes.c_void_p)]


_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_on_error = _ERROR_HANDLER(lambda _dpy, _ev: 0)  # kept alive here: Xlib holds only the pointer
_libs = None


def _load():
    global _libs
    if _libs is None:
        x11 = ctypes.util.find_library("X11")
        if not x11:
            _libs = (None, None)
            return _libs
        x = ctypes.CDLL(x11)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x.XDisplayWidth.argtypes = x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XGetAtomName.restype = ctypes.c_void_p
        x.XGetAtomName.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
        x.XSetErrorHandler(_on_error)
        r = None
        xrr = ctypes.util.find_library("Xrandr")
        if xrr:
            r = ctypes.CDLL(xrr)
            r.XRRQueryExtension.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            r.XRRQueryVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            r.XRRGetMonitors.restype = ctypes.POINTER(_MonitorInfo)
            r.XRRGetMonitors.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            r.XRRFreeMonitors.argtypes = [ctypes.POINTER(_MonitorInfo)]
        _libs = (x, r)
    return _libs


def _open(display):
    x, _r = _load()
    if x is None:
        return None
    dpy = x.XOpenDisplay(display.encode() if display else None)
    return dpy or None


def screen_size(display):
    """(width, height) of the X screen, or None when there is no desktop."""
    dpy = _open(display)
    if dpy is None:
        return None
    x, _r = _load()
    try:
        s = x.XDefaultScreen(dpy)
        return x.XDisplayWidth(dpy, s), x.XDisplayHeight(dpy, s)
    finally:
        x.XCloseDisplay(dpy)


def _monitors(x, r, dpy):
    if r is None:
        return []
    a, b = ctypes.c_int(), ctypes.c_int()
    if not r.XRRQueryExtension(dpy, ctypes.byref(a), ctypes.byref(b)):
        return []
    if not r.XRRQueryVersion(dpy, ctypes.byref(a), ctypes.byref(b)) or (a.value, b.value) < (1, 5):
        return []
    n = ctypes.c_int()
    arr = r.XRRGetMonitors(dpy, x.XDefaultRootWindow(dpy), 1, ctypes.byref(n))
    if not arr:
        return []
    out = []
    try:
        for i in range(n.value):
            m = arr[i]
            p = x.XGetAtomName(dpy, m.name) if m.name else None
            name = ctypes.string_at(p).decode("utf-8", "replace") if p else "monitor-%d" % (i + 1)
            if p:
                x.XFree(p)
            if m.width > 0 and m.height > 0:
                out.append({"name": name, "x": m.x, "y": m.y, "width": m.width, "height": m.height,
                            "primary": bool(m.primary)})
    finally:
        r.XRRFreeMonitors(arr)
    out.sort(key=lambda m: (m["x"], m["y"]))      # left to right, as they stand on the desk
    return out


def describe(display):
    """{"screen": [w, h], "monitors": [{name, x, y, width, height, primary}]} or None (no desktop).
    Without RandR monitors the whole screen is the one monitor ("screen")."""
    dpy = _open(display)
    if dpy is None:
        return None
    x, r = _load()
    try:
        s = x.XDefaultScreen(dpy)
        w, h = x.XDisplayWidth(dpy, s), x.XDisplayHeight(dpy, s)
        mons = _monitors(x, r, dpy) or [{"name": "screen", "x": 0, "y": 0, "width": w, "height": h, "primary": True}]
        return {"screen": [w, h], "monitors": mons}
    finally:
        x.XCloseDisplay(dpy)


def pick(desc, want):
    """The area to show for the setting `want` ("primary", "all" or a monitor's name) ->
    {"name", "x", "y", "width", "height"}; a monitor that is not there gives the primary one."""
    w, h = desc["screen"]
    whole = {"name": "all", "x": 0, "y": 0, "width": w, "height": h}
    mons = desc["monitors"]
    if want == "all" or not mons:
        return whole
    m = next((m for m in mons if m["name"] == want), None) or next((m for m in mons if m["primary"]), mons[0])
    # clipped to the screen (a monitor may hang over its edge while the desktop is rearranged)
    x0, y0 = max(0, m["x"]), max(0, m["y"])
    x1, y1 = min(w, m["x"] + m["width"]), min(h, m["y"] + m["height"])
    if x1 - x0 < 2 or y1 - y0 < 2:
        return whole
    return {"name": m["name"], "x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arstro_remote.screen.xdisplay")
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ""))
    a = ap.parse_args(argv)
    print(json.dumps(describe(a.display)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
