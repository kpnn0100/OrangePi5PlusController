"""`io.*` ops - the IO Control module (IO-01..10).

Publishes the hub topic "io.gpio": the lines this server holds (mode, value, edge counts)
and the latest edge events, so every controller sees a pin change live.
"""

import logging
import os
import re
import select
import threading
import time

from .. import boards
from ..session import OpError
from . import gpio, i2c, spi, sysfs, uart

log = logging.getLogger("arstro.io")

CONSUMER = "arstro-remote"
EVENTS_KEEP = 100
ERRORS = (gpio.GpioError, i2c.I2cError, spi.SpiError, sysfs.SysfsError, uart.UartError)


def parse_bytes(value, what="data"):
    """"12 34 0xab", "1234ab", [0x12, 52] -> bytes."""
    if value is None or value == "":
        return b""
    if isinstance(value, (list, tuple)):
        try:
            return bytes(int(v, 0) if isinstance(v, str) else int(v) for v in value)
        except ValueError as e:
            raise OpError("%s: %s" % (what, e))
    s = str(value).strip()
    parts = [p for p in re.split(r"[\s,;:]+", s) if p]
    try:
        if len(parts) == 1 and not parts[0].lower().startswith("0x") and len(parts[0]) > 2:
            if len(parts[0]) % 2:
                raise ValueError("odd number of hex digits")
            return bytes.fromhex(parts[0])
        return bytes(int(p, 16) for p in parts)
    except ValueError as e:
        raise OpError("%s must be hex bytes like '0a 1b ff' (%s)" % (what, e))


def hexs(data):
    return " ".join("%02x" % b for b in data)


class IoService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.board = boards.detect()
        self._lines = {}             # (chip path, line) -> gpio.Line
        self._lock = threading.RLock()
        self._events = []
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()

    # -------------------------------------------------------------- lifecycle
    def start(self):
        log.info("IO control: board %s", self.board.NAME if self.board else "unknown (generic)")
        self._publish()
        threading.Thread(target=self._watch, name="io-gpio", daemon=True).start()

    def shutdown(self):
        self._stop.set()
        os.write(self._wake_w, b"x")
        with self._lock:
            for ln in self._lines.values():
                ln.close()
            self._lines.clear()

    def handle(self, session, op, msg):
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            raise OpError("unknown op %s" % op)
        try:
            return fn(session, msg)
        except ERRORS as e:
            raise OpError(str(e))

    # ------------------------------------------------------------ gpio state
    def _held(self):
        out = []
        for (path, off), ln in sorted(self._lines.items()):
            try:
                value = ln.get()
            except gpio.GpioError:
                value = None
            out.append(dict(ln.settings, chip=path, line=off, value=value, events=ln.events,
                            pin=self._pin_of(path, off)))
        return out

    def _publish(self):
        with self._lock:
            data = {"held": self._held(), "events": self._events[-20:]}
        self.ctx.hub.publish("io.gpio", data)

    def _watch(self):
        """Edge events (select on the request fds) + input polling for held lines."""
        while not self._stop.is_set():
            with self._lock:
                edge_lines = {ln.fd: ln for ln in self._lines.values() if ln.settings.get("edge") != "none"}
            try:
                r, _, _ = select.select(list(edge_lines) + [self._wake_r], [], [], 0.25)
            except (OSError, ValueError):
                time.sleep(0.1)
                continue
            if self._wake_r in r:
                os.read(self._wake_r, 64)
            changed = False
            with self._lock:
                for fd in r:
                    ln = edge_lines.get(fd)
                    if ln is None or ln.fd is None:
                        continue
                    for ts, kind in ln.read_events():
                        ln.events += 1
                        self._events.append({"chip": ln.path, "line": ln.offset, "edge": kind,
                                             "t": ts / 1e9, "pin": self._pin_of(ln.path, ln.offset)})
                        changed = True
                del self._events[:-EVENTS_KEEP]
            if changed or self._lines:
                self._publish()        # the hub drops unchanged values

    def _pin_of(self, path, off):
        if not self.board:
            return None
        label = self._chip_labels().get(path)
        for p in self.board.HEADER:
            g = p.get("gpio")
            if g and g["line"] == off and label == self.board.GPIO_CHIP_LABEL.format(bank=g["bank"]):
                return p["pin"]
        return None

    _labels = None

    def _chip_labels(self):
        if self._labels is None:
            labels = {}
            for p in gpio.chip_paths():
                try:
                    labels[p] = gpio.chip_info(p)["label"]
                except gpio.GpioError:
                    pass
            if labels or not gpio.chip_paths():
                self._labels = labels
            return labels
        return self._labels

    def _chip(self, msg):
        c = msg.get("chip")
        if c is None:
            raise OpError("chip is required")
        s = str(c)
        for path, label in self._chip_labels().items():
            if s == label:
                return path
        return gpio.chip_path(s)

    # ------------------------------------------------------------------- ops
    def op_io_info(self, _s, _m):
        info = boards.describe(self.board)
        chips = []
        problems = []
        for p in gpio.chip_paths():
            try:
                chips.append(gpio.chip_info(p))
            except gpio.GpioError as e:
                chips.append({"chip": p, "error": str(e).split(": ", 1)[-1]})
                problems.append("GPIO: " + str(e).split(": ", 1)[-1])
        info.update(gpio=chips, i2c=i2c.buses(), spi=spi.devices(), uart=uart.ports(),
                    pwm=sysfs.pwm_list(), leds=sysfs.leds(), adc=sysfs.adc())
        if any(not b["access"] for b in info["i2c"]):
            problems.append("I2C: no access to /dev/i2c-* - run install.sh (group i2c), then log in again")
        if any(not p["writable"] for p in info["pwm"]) or any(not l["writable"] for l in info["leds"]):
            problems.append("PWM/LEDs: read only - run install.sh (group gpio), then log in again")
        info["problems"] = sorted(set(problems))
        return info

    def op_io_gpio_chips(self, _s, _m):
        out = []
        for p in gpio.chip_paths():
            try:
                out.append(gpio.chip_info(p))
            except gpio.GpioError as e:
                out.append({"chip": p, "error": str(e)})
        return {"chips": out}

    def op_io_gpio_lines(self, _s, msg):
        path = self._chip(msg)
        label, lines = gpio.line_infos(path)
        with self._lock:
            for ln in lines:
                held = self._lines.get((path, ln["line"]))
                ln["held"] = held is not None
                if held:
                    ln["value"] = held.get()
                    ln.update(held.settings)
                ln["pin"] = self._pin_of(path, ln["line"])
        return {"chip": path, "label": label, "lines": lines}

    def op_io_gpio_header(self, _s, _m):
        """The board's pin header with each pin's live line state."""
        if not self.board:
            return {"board": None, "pins": []}
        labels = {v: k for k, v in self._chip_labels().items()}
        infos = {}
        pins = []
        for p in self.board.HEADER:
            d = dict(p)
            g = p.get("gpio")
            if g:
                path = labels.get(self.board.GPIO_CHIP_LABEL.format(bank=g["bank"]))
                d["chip"] = path
                if path:
                    if path not in infos:
                        try:
                            infos[path] = gpio.line_infos(path)[1]
                        except gpio.GpioError as e:
                            infos[path] = str(e)
                    li = infos[path]
                    if isinstance(li, list):
                        d["state"] = li[g["line"]]
                    else:
                        d["error"] = li
                    with self._lock:
                        held = self._lines.get((path, g["line"]))
                        if held:
                            d["held"] = dict(held.settings, value=held.get())
            pins.append(d)
        return {"board": self.board.NAME, "pins": pins, "enable_hint": self.board.ENABLE_HINT}

    def op_io_gpio_request(self, session, msg):
        path = self._chip(msg)
        off = int(msg.get("line"))
        args = dict(mode=msg.get("mode", "input"), bias=msg.get("bias", "as-is"),
                    drive=msg.get("drive", "push-pull"), active_low=bool(msg.get("active_low")),
                    edge=msg.get("edge", "none"), debounce_us=int(msg.get("debounce_us") or 0),
                    value=int(bool(msg.get("value", 0))))
        with self._lock:
            ln = self._lines.get((path, off))
            if ln:
                ln.reconfigure(**args)
            else:
                ln = gpio.Line(path, off, CONSUMER, **args)
                self._lines[(path, off)] = ln
            value = ln.get()
        log.info("gpio %s line %d -> %s by session %d (%s)", path, off,
                 ", ".join("%s=%s" % kv for kv in args.items()), session.num, session.controller)
        os.write(self._wake_w, b"x")
        self._publish()
        return dict(ln.settings, chip=path, line=off, value=value)

    def _held_line(self, msg):
        path = self._chip(msg)
        off = int(msg.get("line"))
        ln = self._lines.get((path, off))
        if not ln:
            raise OpError("line %d of %s is not requested - request it first (io.gpio.request)" % (off, path))
        return path, off, ln

    def op_io_gpio_set(self, session, msg):
        with self._lock:
            path, off, ln = self._held_line(msg)
            ln.set(int(bool(msg.get("value"))))
            v = ln.get()
        log.info("gpio %s line %d = %d by session %d (%s)", path, off, v, session.num, session.controller)
        self._publish()
        return {"chip": path, "line": off, "value": v}

    def op_io_gpio_get(self, _s, msg):
        with self._lock:
            path, off, ln = self._held_line(msg)
            return {"chip": path, "line": off, "value": ln.get()}

    def op_io_gpio_release(self, session, msg):
        with self._lock:
            path, off, ln = self._held_line(msg)
            ln.close()
            del self._lines[(path, off)]
        log.info("gpio %s line %d released by session %d (%s)", path, off, session.num, session.controller)
        os.write(self._wake_w, b"x")
        self._publish()
        return {}

    def op_io_gpio_clear_events(self, _s, _m):
        with self._lock:
            self._events.clear()
            for ln in self._lines.values():
                ln.events = 0
        self._publish()
        return {}

    # I2C
    def op_io_i2c_buses(self, _s, _m):
        return {"buses": i2c.buses()}

    def op_io_i2c_scan(self, session, msg):
        bus = msg.get("bus")
        r = i2c.scan(bus, int(msg.get("first", 0x08)), int(msg.get("last", 0x77)))
        log.info("i2c scan bus %s by session %d (%s): %s", bus, session.num, session.controller,
                 " ".join("%02x" % a for a in r["found"]) or "nothing")
        return r

    def op_io_i2c_transfer(self, session, msg):
        bus, addr = msg.get("bus"), msg.get("addr")
        w = parse_bytes(msg.get("write"), "write")
        n = int(msg.get("read") or 0)
        data = i2c.transfer(bus, addr, w, n, bool(msg.get("ten_bit")))
        log.info("i2c bus %s addr %s: wrote [%s] read %d by session %d (%s)", bus, addr, hexs(w), n,
                 session.num, session.controller)
        return {"read": hexs(data), "bytes": list(data)}

    def op_io_i2c_dump(self, _s, msg):
        data = i2c.dump(msg.get("bus"), msg.get("addr"), int(msg.get("start", 0)), int(msg.get("count", 256)),
                        int(msg.get("reg_bytes", 1)))
        return {"start": int(msg.get("start", 0)), "data": hexs(data), "bytes": list(data)}

    # SPI
    def op_io_spi_devices(self, _s, _m):
        return {"devices": spi.devices()}

    def op_io_spi_transfer(self, session, msg):
        tx = parse_bytes(msg.get("tx"), "tx")
        rx = spi.transfer(msg.get("device"), tx, mode=int(msg.get("mode", 0)),
                          speed_hz=int(msg.get("speed_hz", 1_000_000)), bits=int(msg.get("bits", 8)),
                          lsb_first=bool(msg.get("lsb_first")), cs_high=bool(msg.get("cs_high")),
                          delay_us=int(msg.get("delay_us", 0)))
        log.info("spi %s: %d bytes by session %d (%s)", msg.get("device"), len(tx), session.num, session.controller)
        return {"rx": hexs(rx), "bytes": list(rx)}

    # UART
    def op_io_uart_ports(self, _s, _m):
        return {"ports": uart.ports(), "bauds": uart.BAUDS}

    def op_io_uart_open(self, session, msg):
        port = msg.get("port")
        if not port:
            raise OpError("port is required")
        settings = {"baud": int(msg.get("baud", 115200)), "data_bits": int(msg.get("data_bits", 8)),
                    "parity": msg.get("parity", "none"), "stop_bits": int(msg.get("stop_bits", 1)),
                    "flow": msg.get("flow", "none")}
        for t in self.ctx.terms.list():
            if t.get("kind") == "serial" and t.get("port") == os.path.basename(str(port)):
                return {"term": t["term"], "already_open": True, "settings": t["settings"]}

        def factory(term_id, cols, rows, pool, opened_by, ephemeral_owner=None):
            return uart.SerialTerminal(term_id, cols, rows, pool, opened_by, ephemeral_owner,
                                       port=port, settings=settings)
        tid = self.ctx.terms.open(session, int(msg.get("cols", 100)), int(msg.get("rows", 30)), factory=factory)
        log.info("serial console %d on %s %s by session %d (%s)", tid, port, settings, session.num, session.controller)
        return {"term": tid, "settings": self._serial(tid).port_settings}

    def _serial(self, term_id):
        t = self.ctx.terms._get(int(term_id))
        if not isinstance(t, uart.SerialTerminal):
            raise OpError("terminal %s is not a serial console" % term_id)
        return t

    def op_io_uart_config(self, _s, msg):
        t = self._serial(msg.get("term"))
        keys = ("baud", "data_bits", "parity", "stop_bits", "flow")
        r = t.reconfigure(**{k: msg[k] for k in keys if k in msg})
        self.ctx.terms._changed()
        return {"settings": r}

    def op_io_uart_modem(self, _s, msg):
        t = self._serial(msg.get("term"))
        if "dtr" in msg or "rts" in msg:
            return t.set_modem(msg.get("dtr"), msg.get("rts"))
        return t.modem()

    def op_io_uart_break(self, _s, msg):
        return self._serial(msg.get("term")).send_break()

    def op_io_uart_send(self, _s, msg):
        t = self._serial(msg.get("term"))
        data = parse_bytes(msg.get("hex"), "hex") if msg.get("hex") is not None else str(msg.get("text", "")).encode()
        t.write(data)
        return {"sent": len(data)}

    # PWM, LEDs, ADC
    def op_io_pwm_list(self, _s, _m):
        return {"chips": sysfs.pwm_list()}

    def op_io_pwm_set(self, session, msg):
        period = msg.get("period_ns")
        if period is None and msg.get("freq_hz"):
            f = float(msg["freq_hz"])
            if f <= 0:
                raise OpError("frequency must be > 0 Hz")
            period = int(round(1e9 / f))
        duty = msg.get("duty_ns")
        if duty is None and msg.get("duty_pct") is not None:
            p = float(msg["duty_pct"])
            if not 0 <= p <= 100:
                raise OpError("duty must be 0..100 %")
            base = period
            if base is None:
                cur = next((c for c in sysfs.pwm_list() if str(c["chip"]) == str(msg.get("chip"))), None)
                base = cur and cur["channels"][int(msg.get("channel", 0))].get("period_ns")
            if not base:
                raise OpError("set a frequency / period first")
            duty = int(round(int(base) * p / 100))
        r = sysfs.pwm_set(msg.get("chip"), int(msg.get("channel", 0)), period, duty, msg.get("polarity"),
                          msg.get("enabled"))
        log.info("pwm chip %s ch %s -> %s by session %d (%s)", msg.get("chip"), msg.get("channel", 0), r,
                 session.num, session.controller)
        self.ctx.hub.publish("io.pwm", sysfs.pwm_list())
        return r

    def op_io_pwm_unexport(self, _s, msg):
        sysfs.pwm_unexport(msg.get("chip"), int(msg.get("channel", 0)))
        self.ctx.hub.publish("io.pwm", sysfs.pwm_list())
        return {}

    def op_io_led_list(self, _s, _m):
        return {"leds": sysfs.leds()}

    def op_io_led_set(self, session, msg):
        r = sysfs.led_set(msg.get("name"), msg.get("brightness"), msg.get("trigger"))
        log.info("led %s -> %s/%s by session %d (%s)", msg.get("name"), r["brightness"], r["trigger"],
                 session.num, session.controller)
        return r

    def op_io_adc_read(self, _s, _m):
        return {"devices": sysfs.adc()}
