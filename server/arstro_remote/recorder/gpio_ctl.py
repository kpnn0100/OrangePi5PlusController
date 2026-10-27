"""Physical controls on the rig (REC-16): a record button and a tally light on GPIO lines.

Settings `gpio.button` / `gpio.tally` name a line as "gpiochipN:LINE" ("" = not used); the
Camera settings offer the board's header pins (IO Control's map) to pick from. The button
is an input with the pull-up on and active low by default (a push button to ground):
each press starts or stops recording, whatever controller started it. The tally is an
output, on while recording (and blinking while a take is being finished).

Lines are requested with the consumer "arstro-recorder", so IO Control shows them as used
and cannot drive them by mistake. Errors (no such chip, line held elsewhere, no permission)
are reported in the recorder status, never raised at the user's other actions.
"""

import logging
import os
import re
import select
import threading
import time

log = logging.getLogger("arstro.recorder")

CONSUMER = "arstro-recorder"
DEBOUNCE_S = 0.3
PIN = re.compile(r"^(gpiochip\d+|\d+):(\d{1,3})$")


def parse_pin(text):
    """'gpiochip1:5' / '1:5' -> ('/dev/gpiochip1', 5); '' -> None."""
    text = (text or "").strip()
    if not text:
        return None
    m = PIN.match(text)
    if not m:
        raise ValueError("a GPIO line is chip:line, e.g. gpiochip1:5")
    chip = m.group(1)
    return ("/dev/" + chip if chip.startswith("gpiochip") else "/dev/gpiochip" + chip), int(m.group(2))


class RecorderGpio:
    def __init__(self, on_press, gpio_module=None):
        if gpio_module is None:
            from ..hwio import gpio as gpio_module
        self.gpio = gpio_module
        self.on_press = on_press
        self.button = self.tally = None
        self.state = {"button": {"pin": "", "ok": None, "error": None, "presses": 0},
                      "tally": {"pin": "", "ok": None, "error": None, "on": False}}
        self._cfg = None
        self._lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()
        self._stop = False
        self._last_press = 0.0
        self._tally_mode = "off"
        self._thread = threading.Thread(target=self._loop, name="recorder-gpio", daemon=True)
        self._thread.start()

    def configure(self, cfg):
        """cfg: the settings' gpio block. Re-requests only what changed."""
        cfg = dict(cfg or {})
        if cfg == self._cfg:
            return self.state
        with self._lock:
            old = self._cfg or {}
            self._cfg = cfg
            if (old.get("button"), old.get("button_active_low")) != (cfg.get("button"), cfg.get("button_active_low")):
                self._close("button")
                self.button = self._request("button", cfg.get("button"), mode="input", bias="pull-up"
                                            if cfg.get("button_active_low", True) else "pull-down",
                                            active_low=cfg.get("button_active_low", True), edge="rising")
            if (old.get("tally"), old.get("tally_active_low")) != (cfg.get("tally"), cfg.get("tally_active_low")):
                self._close("tally")
                self.tally = self._request("tally", cfg.get("tally"), mode="output",
                                           active_low=cfg.get("tally_active_low", False))
                self._apply_tally()
        os.write(self._wake_w, b"x")
        return self.state

    def _request(self, which, pin, **kw):
        st = self.state[which]
        st.update(pin=pin or "", ok=None, error=None)
        try:
            where = parse_pin(pin)
        except ValueError as e:
            st.update(ok=False, error=str(e))
            return None
        if not where:
            return None
        try:
            debounce = 10000 if which == "button" else 0
            line = self.gpio.Line(where[0], where[1], CONSUMER, debounce_us=debounce, **kw)
        except Exception as e:                       # GpioError, OSError: say it, keep going
            st.update(ok=False, error=str(e))
            log.warning("recorder %s on %s: %s", which, pin, e)
            return None
        st["ok"] = True
        log.info("recorder %s on %s", which, pin)
        return line

    def _close(self, which):
        line = getattr(self, which)
        if line:
            try:
                if which == "tally":
                    line.set(0)
            except Exception:
                pass
            line.close()
        setattr(self, which, None)

    # tally: "off" | "on" (recording) | "busy" (a take being finished: blinks)
    def set_tally(self, mode):
        if mode != self._tally_mode:
            self._tally_mode = mode
            with self._lock:
                self._apply_tally()

    def _apply_tally(self, phase=True):
        on = self._tally_mode == "on" or (self._tally_mode == "busy" and phase)
        self.state["tally"]["on"] = on
        if self.tally:
            try:
                self.tally.set(1 if on else 0)
            except Exception as e:
                self.state["tally"].update(ok=False, error=str(e))

    def _loop(self):
        blink = False
        while not self._stop:
            fds = [self._wake_r]
            with self._lock:
                btn = self.button
            if btn and btn.fd is not None:
                fds.append(btn.fd)
            try:
                r, _, _ = select.select(fds, [], [], 0.25)
            except (OSError, ValueError):
                time.sleep(0.25)
                continue
            if self._wake_r in r:
                os.read(self._wake_r, 64)
            if btn and btn.fd in r:
                try:
                    events = btn.read_events()
                except Exception:
                    events = []
                if any(kind == "rising" for _, kind in events):
                    now = time.monotonic()
                    if now - self._last_press >= DEBOUNCE_S:
                        self._last_press = now
                        self.state["button"]["presses"] += 1
                        try:
                            self.on_press()
                        except Exception as e:
                            log.warning("record button: %s", e)
            if self._tally_mode == "busy":
                blink = not blink
                with self._lock:
                    self._apply_tally(blink)

    def close(self):
        self._stop = True
        os.write(self._wake_w, b"x")
        with self._lock:
            self._close("button")
            self._close("tally")
