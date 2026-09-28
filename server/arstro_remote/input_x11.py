"""Mouse and keyboard injection into the X11 desktop through the XTEST extension.

Uses ctypes against libX11/libXtst, so it needs no root and no extra packages -
only access to the X display (DISPLAY + XAUTHORITY of the logged-in user).
"""

import ctypes
import ctypes.util
import json
import logging
import os
import select
import subprocess
import sys
import threading
import time

log = logging.getLogger("arstro.input")

NoSymbol = 0

MODIFIERS = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "meta": "Super_L",
    "win": "Super_L",
    "altgr": "ISO_Level3_Shift",
}

BUTTONS = {"left": 1, "middle": 2, "right": 3, "back": 8, "forward": 9}

# Friendly aliases the app may send in addition to raw X keysym names.
KEY_ALIASES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
    "insert": "Insert", "home": "Home", "end": "End", "pageup": "Page_Up",
    "pagedown": "Page_Down", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "space": "space", "printscreen": "Print", "menu": "Menu",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute", "playpause": "XF86AudioPlay",
}


class InputUnavailable(Exception):
    pass


_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_IO_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)


def _on_x_error(_dpy, _event):
    log.warning("X protocol error (ignored)")
    return 0


def _on_x_io_error(_dpy):
    # Xlib would call exit() right after this returns; the launcher restarts us.
    log.error("lost connection to the X server, exiting")
    logging.shutdown()
    os._exit(3)


_error_cb = _ERROR_HANDLER(_on_x_error)
_io_error_cb = _IO_ERROR_HANDLER(_on_x_io_error)


def keysym_for_char(ch):
    if ch == "\n" or ch == "\r":
        return "Return"
    if ch == "\t":
        return "Tab"
    if ch == "\b":
        return "BackSpace"
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp  # Latin-1 keysyms equal the code point
    return 0x01000000 + cp  # Unicode keysym


class X11Input:
    RETRY_INTERVAL = 3.0

    def __init__(self, display=None):
        self._display_name = display or os.environ.get("DISPLAY") or ":0"
        if "XAUTHORITY" not in os.environ:
            xauth = os.path.expanduser("~/.Xauthority")
            if os.path.exists(xauth):
                os.environ["XAUTHORITY"] = xauth
        self._lock = threading.RLock()
        self._dpy = None
        self._last_try = 0.0
        self._pressed_buttons = set()
        self._pressed_keys = set()
        self._scroll_acc = [0.0, 0.0]
        self._move_acc = [0.0, 0.0]
        self._scratch = None
        self._load_libs()

    # ------------------------------------------------------------------ setup
    def _load_libs(self):
        self.x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self.xt = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
        x, xt = self.x, self.xt
        vp, ul, ui, i = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_int
        x.XOpenDisplay.restype = vp
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XCloseDisplay.argtypes = [vp]
        x.XFlush.argtypes = [vp]
        x.XSync.argtypes = [vp, i]
        x.XDefaultScreen.argtypes = [vp]
        x.XDisplayWidth.argtypes = [vp, i]
        x.XDisplayHeight.argtypes = [vp, i]
        x.XDefaultRootWindow.restype = ul
        x.XDefaultRootWindow.argtypes = [vp]
        x.XStringToKeysym.restype = ul
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [vp, ul]
        x.XkbKeycodeToKeysym.restype = ul
        x.XkbKeycodeToKeysym.argtypes = [vp, ctypes.c_ubyte, i, i]
        x.XDisplayKeycodes.argtypes = [vp, ctypes.POINTER(i), ctypes.POINTER(i)]
        x.XGetKeyboardMapping.restype = ctypes.POINTER(ul)
        x.XGetKeyboardMapping.argtypes = [vp, ctypes.c_ubyte, i, ctypes.POINTER(i)]
        x.XChangeKeyboardMapping.argtypes = [vp, i, i, ctypes.POINTER(ul), i]
        x.XFree.argtypes = [vp]
        x.XQueryPointer.argtypes = [vp, ul, ctypes.POINTER(ul), ctypes.POINTER(ul),
                                    ctypes.POINTER(i), ctypes.POINTER(i), ctypes.POINTER(i),
                                    ctypes.POINTER(i), ctypes.POINTER(ui)]
        x.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
        x.XSetErrorHandler.restype = vp
        x.XSetIOErrorHandler.argtypes = [_IO_ERROR_HANDLER]
        x.XSetIOErrorHandler.restype = vp
        xt.XTestQueryExtension.argtypes = [vp] + [ctypes.POINTER(i)] * 4
        xt.XTestFakeRelativeMotionEvent.argtypes = [vp, i, i, ul]
        xt.XTestFakeMotionEvent.argtypes = [vp, i, i, i, ul]
        xt.XTestFakeButtonEvent.argtypes = [vp, ui, i, ul]
        xt.XTestFakeKeyEvent.argtypes = [vp, ui, i, ul]
        x.XSetErrorHandler(_error_cb)
        x.XSetIOErrorHandler(_io_error_cb)

    def _display(self):
        if self._dpy:
            return self._dpy
        now = time.monotonic()
        if now - self._last_try < self.RETRY_INTERVAL:
            raise InputUnavailable("X display %s not available" % self._display_name)
        self._last_try = now
        dpy = self.x.XOpenDisplay(self._display_name.encode())
        if not dpy:
            raise InputUnavailable("cannot open X display %s" % self._display_name)
        a, b, c, d = (ctypes.c_int() for _ in range(4))
        if not self.xt.XTestQueryExtension(dpy, ctypes.byref(a), ctypes.byref(b),
                                           ctypes.byref(c), ctypes.byref(d)):
            self.x.XCloseDisplay(dpy)
            raise InputUnavailable("X server has no XTEST extension")
        self._dpy = dpy
        log.info("opened X display %s (XTEST %d.%d)", self._display_name, c.value, d.value)
        return dpy

    @property
    def available(self):
        try:
            with self._lock:
                self._display()
            return True
        except InputUnavailable:
            return False

    @property
    def backend(self):
        return "xtest"

    # ------------------------------------------------------------------ mouse
    def move(self, dx, dy):
        with self._lock:
            dpy = self._display()
            self._move_acc[0] += float(dx)
            self._move_acc[1] += float(dy)
            ix, iy = int(self._move_acc[0]), int(self._move_acc[1])
            if ix == 0 and iy == 0:
                return
            self._move_acc[0] -= ix
            self._move_acc[1] -= iy
            self.xt.XTestFakeRelativeMotionEvent(dpy, ix, iy, 0)
            self.x.XFlush(dpy)

    def move_to(self, x, y):
        with self._lock:
            dpy = self._display()
            self.xt.XTestFakeMotionEvent(dpy, -1, int(x), int(y), 0)
            self.x.XFlush(dpy)

    def button(self, button, action="click"):
        b = BUTTONS.get(button, button) if isinstance(button, str) else int(button)
        if not isinstance(b, int) or not 1 <= b <= 9:
            raise ValueError("bad button %r" % (button,))
        with self._lock:
            dpy = self._display()
            if action == "down":
                self.xt.XTestFakeButtonEvent(dpy, b, 1, 0)
                self._pressed_buttons.add(b)
            elif action == "up":
                self.xt.XTestFakeButtonEvent(dpy, b, 0, 0)
                self._pressed_buttons.discard(b)
            elif action in ("click", "double"):
                for _ in range(2 if action == "double" else 1):
                    self.xt.XTestFakeButtonEvent(dpy, b, 1, 0)
                    self.xt.XTestFakeButtonEvent(dpy, b, 0, 0)
                    self.x.XFlush(dpy)
                self._pressed_buttons.discard(b)
            else:
                raise ValueError("bad button action %r" % action)
            self.x.XFlush(dpy)

    def scroll(self, dx=0.0, dy=0.0):
        """dy > 0 scrolls down, dx > 0 scrolls right. Fractions accumulate."""
        with self._lock:
            dpy = self._display()
            self._scroll_acc[0] += float(dx)
            self._scroll_acc[1] += float(dy)
            for axis, (neg, pos) in ((1, (4, 5)), (0, (6, 7))):
                steps = int(self._scroll_acc[axis])
                if not steps:
                    continue
                self._scroll_acc[axis] -= steps
                btn = pos if steps > 0 else neg
                for _ in range(min(abs(steps), 50)):
                    self.xt.XTestFakeButtonEvent(dpy, btn, 1, 0)
                    self.xt.XTestFakeButtonEvent(dpy, btn, 0, 0)
            self.x.XFlush(dpy)

    def pointer(self):
        with self._lock:
            dpy = self._display()
            root = self.x.XDefaultRootWindow(dpy)
            r, c = ctypes.c_ulong(), ctypes.c_ulong()
            rx, ry, wx, wy = (ctypes.c_int() for _ in range(4))
            mask = ctypes.c_uint()
            self.x.XQueryPointer(dpy, root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx),
                                 ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
            scr = self.x.XDefaultScreen(dpy)
            return {"x": rx.value, "y": ry.value, "mask": mask.value,
                    "width": self.x.XDisplayWidth(dpy, scr), "height": self.x.XDisplayHeight(dpy, scr)}

    # --------------------------------------------------------------- keyboard
    def _keysym(self, key):
        if isinstance(key, int):
            return key
        if len(key) == 1:
            return self._keysym(keysym_for_char(key))
        name = KEY_ALIASES.get(key.lower(), key)
        sym = self.x.XStringToKeysym(name.encode())
        if sym == NoSymbol:
            raise ValueError("unknown key %r" % key)
        return sym

    def _find_scratch_keycode(self, dpy):
        if self._scratch:
            return self._scratch
        lo, hi = ctypes.c_int(), ctypes.c_int()
        self.x.XDisplayKeycodes(dpy, ctypes.byref(lo), ctypes.byref(hi))
        per = ctypes.c_int()
        count = hi.value - lo.value + 1
        syms = self.x.XGetKeyboardMapping(dpy, lo.value, count, ctypes.byref(per))
        try:
            for idx in range(count - 1, -1, -1):  # prefer high, rarely used codes
                row = [syms[idx * per.value + j] for j in range(per.value)]
                if all(s == NoSymbol for s in row):
                    self._scratch = lo.value + idx
                    return self._scratch
        finally:
            self.x.XFree(syms)
        raise InputUnavailable("no free keycode to type this character")

    def _resolve(self, dpy, sym):
        """Return (keycode, extra_modifier_keysym_or_None, needs_remap)."""
        kc = self.x.XKeysymToKeycode(dpy, sym)
        if kc:
            for level, mod in ((0, None), (1, "Shift_L"), (2, "ISO_Level3_Shift")):
                if self.x.XkbKeycodeToKeysym(dpy, kc, 0, level) == sym:
                    return kc, mod, False
            return kc, None, False
        return self._find_scratch_keycode(dpy), None, True

    def _fake_key(self, dpy, keycode, down):
        self.xt.XTestFakeKeyEvent(dpy, keycode, 1 if down else 0, 0)
        if down:
            self._pressed_keys.add(keycode)
        else:
            self._pressed_keys.discard(keycode)

    def _tap_sym(self, dpy, sym, mods=()):
        kc, extra, remap = self._resolve(dpy, sym)
        if remap:
            arr = (ctypes.c_ulong * 1)(sym)
            self.x.XChangeKeyboardMapping(dpy, kc, 1, arr, 1)
            self.x.XSync(dpy, 0)
            time.sleep(0.02)  # let clients process MappingNotify
        mod_codes = []
        for m in list(mods) + ([extra] if extra else []):
            mkc = self.x.XKeysymToKeycode(dpy, self._keysym(m))
            if mkc and mkc not in mod_codes:
                mod_codes.append(mkc)
        for mkc in mod_codes:
            self._fake_key(dpy, mkc, True)
        self._fake_key(dpy, kc, True)
        self._fake_key(dpy, kc, False)
        for mkc in reversed(mod_codes):
            self._fake_key(dpy, mkc, False)
        if remap:
            self.x.XSync(dpy, 0)
            time.sleep(0.03)
            arr = (ctypes.c_ulong * 1)(NoSymbol)
            self.x.XChangeKeyboardMapping(dpy, kc, 1, arr, 1)
        self.x.XFlush(dpy)

    def key(self, key, mods=(), action="press"):
        mod_syms = [MODIFIERS.get(m.lower(), m) for m in (mods or [])]
        with self._lock:
            dpy = self._display()
            sym = self._keysym(key)
            if action == "press":
                self._tap_sym(dpy, sym, mod_syms)
                return
            kc, _extra, remap = self._resolve(dpy, sym)
            if remap:
                raise ValueError("key %r cannot be held" % (key,))
            if action == "down":
                self._fake_key(dpy, kc, True)
            elif action == "up":
                self._fake_key(dpy, kc, False)
            else:
                raise ValueError("bad key action %r" % action)
            self.x.XFlush(dpy)

    def modifier(self, name, down):
        self.key(MODIFIERS.get(name.lower(), name), action="down" if down else "up")

    def text(self, text, mods=()):
        mod_syms = [MODIFIERS.get(m.lower(), m) for m in (mods or [])]
        with self._lock:
            dpy = self._display()
            for ch in text[:4096]:
                sym = keysym_for_char(ch)
                if isinstance(sym, str):
                    sym = self._keysym(sym)
                self._tap_sym(dpy, sym, mod_syms)
                self.x.XSync(dpy, 0)

    def release_all(self):
        """Release anything a disconnected client left pressed (drag, Ctrl, ...)."""
        with self._lock:
            if not self._dpy:
                return
            for b in list(self._pressed_buttons):
                self.xt.XTestFakeButtonEvent(self._dpy, b, 0, 0)
            for kc in list(self._pressed_keys):
                self.xt.XTestFakeKeyEvent(self._dpy, kc, 0, 0)
            if self._pressed_buttons or self._pressed_keys:
                log.info("released %d buttons / %d keys", len(self._pressed_buttons), len(self._pressed_keys))
            self._pressed_buttons.clear()
            self._pressed_keys.clear()
            self.x.XFlush(self._dpy)


class NullInput:
    backend = "none"
    available = False

    def __getattr__(self, name):
        def fail(*_a, **_k):
            raise InputUnavailable("no input backend (X11/XTEST libraries missing)")
        return fail

    def release_all(self):
        pass


class InputProxy:
    """Runs X11Input in a child process.

    Xlib terminates the process on an X I/O error (X server restart, logout), so
    doing X calls in the daemon itself would take down every Bluetooth session and
    every shell with it. The helper dies instead and is respawned on the next call.
    """

    RESPAWN_INTERVAL = 2.0
    backend = "xtest"

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._buf = b""
        self._last_spawn = 0.0

    def _spawn(self):
        now = time.monotonic()
        if now - self._last_spawn < self.RESPAWN_INTERVAL:
            raise InputUnavailable("X input helper restarting")
        self._last_spawn = now
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self._proc = subprocess.Popen([sys.executable, "-m", "arstro_remote.input_x11", "--serve"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, bufsize=0)
        self._buf = b""
        log.info("X input helper started (pid %d)", self._proc.pid)

    def _kill(self):
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._proc = None

    def _readline(self, timeout):
        deadline = time.monotonic() + timeout
        fd = self._proc.stdout.fileno()
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise EOFError
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def _call(self, method, *args, timeout=5.0, **kwargs):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                if self._proc is not None:
                    log.warning("X input helper exited (code %s)", self._proc.returncode)
                self._spawn()
            try:
                self._proc.stdin.write((json.dumps({"m": method, "a": args, "k": kwargs}) + "\n").encode())
                resp = json.loads(self._readline(timeout))
            except (OSError, EOFError, ValueError):
                self._kill()
                raise InputUnavailable("X input helper died (X server restarted?)")
            except TimeoutError:
                log.warning("X input helper timed out on %s, restarting it", method)
                self._kill()
                raise InputUnavailable("X input helper not responding")
        if resp.get("ok"):
            return resp.get("r")
        if resp.get("t") == "value":
            raise ValueError(resp.get("e"))
        raise InputUnavailable(resp.get("e"))

    @property
    def available(self):
        try:
            return bool(self._call("available"))
        except InputUnavailable:
            return False

    def move(self, dx, dy):
        self._call("move", dx, dy)

    def move_to(self, x, y):
        self._call("move_to", x, y)

    def button(self, button, action="click"):
        self._call("button", button, action)

    def scroll(self, dx=0.0, dy=0.0):
        self._call("scroll", dx, dy)

    def pointer(self):
        return self._call("pointer")

    def key(self, key, mods=(), action="press"):
        self._call("key", key, list(mods or ()), action)

    def text(self, text, mods=()):
        self._call("text", text, list(mods or ()), timeout=10 + 0.08 * len(text))

    def release_all(self):
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._call("release_all")
        except InputUnavailable:
            pass


def serve():
    """Helper process main loop: one JSON request per line on stdin."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s arstro.input-helper: %(message)s")
    inp = X11Input()
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        try:
            req = json.loads(raw)
            if req["m"] == "available":
                r = inp.available
            elif req["m"] in ("move", "move_to", "button", "scroll", "pointer", "key", "text", "release_all"):
                r = getattr(inp, req["m"])(*req.get("a", []), **req.get("k", {}))
            else:
                raise ValueError("unknown method %s" % req["m"])
            resp = {"ok": True, "r": r}
        except InputUnavailable as e:
            resp = {"ok": False, "t": "unavailable", "e": str(e)}
        except (ValueError, KeyError, TypeError) as e:
            resp = {"ok": False, "t": "value", "e": str(e).strip("'\"")}
        out.write((json.dumps(resp) + "\n").encode())
        out.flush()


def create_input():
    if not (ctypes.util.find_library("X11") and ctypes.util.find_library("Xtst")):
        log.error("XTEST backend unavailable: libX11/libXtst not found")
        return NullInput()
    return InputProxy()


if __name__ == "__main__" and "--serve" in sys.argv:
    serve()
