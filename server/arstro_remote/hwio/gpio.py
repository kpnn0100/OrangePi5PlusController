"""GPIO through the Linux GPIO character device, uAPI v2 (/dev/gpiochipN) - IO-02/03.

No libgpiod needed: the ioctls are called directly. Lines the server requests stay
requested (an output keeps its level) until they are released or the server stops.
"""

import ctypes
import errno
import fcntl
import glob
import os
import struct

# ------------------------------------------------------------------- uAPI (linux/gpio.h)
NAME_SIZE = 32
LINES_MAX = 64
NUM_ATTRS_MAX = 10

FLAG_USED = 1 << 0
FLAG_ACTIVE_LOW = 1 << 1
FLAG_INPUT = 1 << 2
FLAG_OUTPUT = 1 << 3
FLAG_EDGE_RISING = 1 << 4
FLAG_EDGE_FALLING = 1 << 5
FLAG_OPEN_DRAIN = 1 << 6
FLAG_OPEN_SOURCE = 1 << 7
FLAG_BIAS_PULL_UP = 1 << 8
FLAG_BIAS_PULL_DOWN = 1 << 9
FLAG_BIAS_DISABLED = 1 << 10
FLAG_EVENT_CLOCK_REALTIME = 1 << 11

ATTR_FLAGS = 1
ATTR_OUTPUT_VALUES = 2
ATTR_DEBOUNCE = 3

EVENT_RISING = 1
EVENT_FALLING = 2


class ChipInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * NAME_SIZE), ("label", ctypes.c_char * NAME_SIZE),
                ("lines", ctypes.c_uint32)]


class _AttrU(ctypes.Union):
    _fields_ = [("flags", ctypes.c_uint64), ("values", ctypes.c_uint64),
                ("debounce_period_us", ctypes.c_uint32)]


class LineAttribute(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("padding", ctypes.c_uint32), ("u", _AttrU)]


class LineConfigAttribute(ctypes.Structure):
    _fields_ = [("attr", LineAttribute), ("mask", ctypes.c_uint64)]


class LineConfig(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("num_attrs", ctypes.c_uint32),
                ("padding", ctypes.c_uint32 * 5), ("attrs", LineConfigAttribute * NUM_ATTRS_MAX)]


class LineRequest(ctypes.Structure):
    _fields_ = [("offsets", ctypes.c_uint32 * LINES_MAX), ("consumer", ctypes.c_char * NAME_SIZE),
                ("config", LineConfig), ("num_lines", ctypes.c_uint32),
                ("event_buffer_size", ctypes.c_uint32), ("padding", ctypes.c_uint32 * 5),
                ("fd", ctypes.c_int32)]


class LineInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * NAME_SIZE), ("consumer", ctypes.c_char * NAME_SIZE),
                ("offset", ctypes.c_uint32), ("num_attrs", ctypes.c_uint32), ("flags", ctypes.c_uint64),
                ("attrs", LineAttribute * NUM_ATTRS_MAX), ("padding", ctypes.c_uint32 * 4)]


class LineValues(ctypes.Structure):
    _fields_ = [("bits", ctypes.c_uint64), ("mask", ctypes.c_uint64)]


EVENT_FMT = "=QIIII24x"          # struct gpio_v2_line_event
EVENT_SIZE = struct.calcsize(EVENT_FMT)

assert ctypes.sizeof(LineInfo) == 256 and ctypes.sizeof(LineRequest) == 592
assert ctypes.sizeof(LineConfig) == 272 and EVENT_SIZE == 48


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (0xB4 << 8) | nr


GET_CHIPINFO = _ioc(2, 0x01, ctypes.sizeof(ChipInfo))
GET_LINEINFO = _ioc(3, 0x05, ctypes.sizeof(LineInfo))
GET_LINE = _ioc(3, 0x07, ctypes.sizeof(LineRequest))
LINE_SET_CONFIG = _ioc(3, 0x0D, ctypes.sizeof(LineConfig))
LINE_GET_VALUES = _ioc(3, 0x0E, ctypes.sizeof(LineValues))
LINE_SET_VALUES = _ioc(3, 0x0F, ctypes.sizeof(LineValues))


class GpioError(Exception):
    pass


def _explain(e, what):
    if e.errno in (errno.EACCES, errno.EPERM):
        return GpioError("%s: permission denied - run install.sh (group gpio), then log in again" % what)
    if e.errno == errno.EBUSY:
        return GpioError("%s: the line is used by a kernel driver or another program" % what)
    return GpioError("%s: %s" % (what, e.strerror or e))


# ------------------------------------------------------------------------ helpers
def chip_paths():
    return sorted(glob.glob("/dev/gpiochip*"), key=lambda p: int(p[len("/dev/gpiochip"):] or 0))


def chip_path(chip):
    """"0", 0, "gpiochip0" or "/dev/gpiochip0" -> /dev/gpiochip0."""
    s = str(chip)
    if s.isdigit():
        s = "/dev/gpiochip" + s
    elif not s.startswith("/"):
        s = "/dev/" + s
    if not s.startswith("/dev/gpiochip") or not s[len("/dev/gpiochip"):].isdigit():
        raise GpioError("no GPIO chip %r" % chip)
    return s


def _open(path):
    try:
        return os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as e:
        raise _explain(e, path)


def chip_info(path):
    fd = _open(path)
    try:
        info = ChipInfo()
        fcntl.ioctl(fd, GET_CHIPINFO, info)
        return {"chip": path, "name": info.name.decode(), "label": info.label.decode(), "lines": info.lines}
    except OSError as e:
        raise _explain(e, path)
    finally:
        os.close(fd)


def _flags_dict(flags):
    direction = "output" if flags & FLAG_OUTPUT else "input" if flags & FLAG_INPUT else "?"
    bias = ("pull-up" if flags & FLAG_BIAS_PULL_UP else "pull-down" if flags & FLAG_BIAS_PULL_DOWN
            else "disabled" if flags & FLAG_BIAS_DISABLED else "as-is")
    drive = "open-drain" if flags & FLAG_OPEN_DRAIN else "open-source" if flags & FLAG_OPEN_SOURCE else "push-pull"
    edge = {FLAG_EDGE_RISING: "rising", FLAG_EDGE_FALLING: "falling",
            FLAG_EDGE_RISING | FLAG_EDGE_FALLING: "both"}.get(flags & (FLAG_EDGE_RISING | FLAG_EDGE_FALLING), "none")
    return {"used": bool(flags & FLAG_USED), "direction": direction, "active_low": bool(flags & FLAG_ACTIVE_LOW),
            "bias": bias, "drive": drive, "edge": edge}


def line_infos(path):
    """Every line of a chip: name, consumer, direction, bias..."""
    fd = _open(path)
    try:
        info = ChipInfo()
        fcntl.ioctl(fd, GET_CHIPINFO, info)
        out = []
        for off in range(info.lines):
            li = LineInfo()
            li.offset = off
            fcntl.ioctl(fd, GET_LINEINFO, li)
            d = {"line": off, "name": li.name.decode(errors="replace"),
                 "consumer": li.consumer.decode(errors="replace")}
            d.update(_flags_dict(li.flags))
            for i in range(li.num_attrs):
                if li.attrs[i].id == ATTR_DEBOUNCE:
                    d["debounce_us"] = li.attrs[i].u.debounce_period_us
            out.append(d)
        return info.label.decode(), out
    except OSError as e:
        raise _explain(e, path)
    finally:
        os.close(fd)


def _config(cfg, mode, bias, drive, active_low, edge, debounce_us, value):
    flags = 0
    if mode == "output":
        flags |= FLAG_OUTPUT
        flags |= {"push-pull": 0, "open-drain": FLAG_OPEN_DRAIN, "open-source": FLAG_OPEN_SOURCE}[drive]
    elif mode == "input":
        flags |= FLAG_INPUT
        flags |= {"none": 0, "rising": FLAG_EDGE_RISING, "falling": FLAG_EDGE_FALLING,
                  "both": FLAG_EDGE_RISING | FLAG_EDGE_FALLING}[edge]
        if edge != "none":
            flags |= FLAG_EVENT_CLOCK_REALTIME
    else:
        raise GpioError("mode must be input or output")
    flags |= {"as-is": 0, "pull-up": FLAG_BIAS_PULL_UP, "pull-down": FLAG_BIAS_PULL_DOWN,
              "disabled": FLAG_BIAS_DISABLED}[bias]
    if active_low:
        flags |= FLAG_ACTIVE_LOW
    cfg.flags = flags
    n = 0
    if mode == "output":
        cfg.attrs[n].attr.id = ATTR_OUTPUT_VALUES
        cfg.attrs[n].attr.u.values = 1 if value else 0
        cfg.attrs[n].mask = 1
        n += 1
    if mode == "input" and debounce_us:
        cfg.attrs[n].attr.id = ATTR_DEBOUNCE
        cfg.attrs[n].attr.u.debounce_period_us = int(debounce_us)
        cfg.attrs[n].mask = 1
        n += 1
    cfg.num_attrs = n


def validate(mode, bias, drive, edge):
    if mode not in ("input", "output"):
        raise GpioError("mode must be input or output")
    if bias not in ("as-is", "pull-up", "pull-down", "disabled"):
        raise GpioError("bias must be as-is, pull-up, pull-down or disabled")
    if drive not in ("push-pull", "open-drain", "open-source"):
        raise GpioError("drive must be push-pull, open-drain or open-source")
    if edge not in ("none", "rising", "falling", "both"):
        raise GpioError("edge must be none, rising, falling or both")


class Line:
    """One requested line (we hold its request fd)."""

    def __init__(self, path, offset, consumer, mode="input", bias="as-is", drive="push-pull",
                 active_low=False, edge="none", debounce_us=0, value=0):
        validate(mode, bias, drive, edge)
        self.path, self.offset = path, int(offset)
        req = LineRequest()
        req.offsets[0] = self.offset
        req.num_lines = 1
        req.consumer = consumer.encode()[:NAME_SIZE - 1]
        req.event_buffer_size = 0
        _config(req.config, mode, bias, drive, active_low, edge, debounce_us, value)
        fd = _open(path)
        try:
            fcntl.ioctl(fd, GET_LINE, req)
        except OSError as e:
            raise _explain(e, "%s line %d" % (path, self.offset))
        finally:
            os.close(fd)
        self.fd = req.fd
        self.settings = {}
        self._remember(mode, bias, drive, active_low, edge, debounce_us)
        self.events = 0
        self.last_events = []

    def _remember(self, mode, bias, drive, active_low, edge, debounce_us):
        self.settings = {"mode": mode, "bias": bias, "drive": drive, "active_low": bool(active_low),
                         "edge": edge if mode == "input" else "none",
                         "debounce_us": int(debounce_us or 0) if mode == "input" else 0}

    def reconfigure(self, mode, bias, drive, active_low, edge, debounce_us, value):
        validate(mode, bias, drive, edge)
        cfg = LineConfig()
        _config(cfg, mode, bias, drive, active_low, edge, debounce_us, value)
        try:
            fcntl.ioctl(self.fd, LINE_SET_CONFIG, cfg)
        except OSError as e:
            raise _explain(e, "%s line %d" % (self.path, self.offset))
        self._remember(mode, bias, drive, active_low, edge, debounce_us)

    def get(self):
        v = LineValues(0, 1)
        try:
            fcntl.ioctl(self.fd, LINE_GET_VALUES, v)
        except OSError as e:
            raise _explain(e, "reading %s line %d" % (self.path, self.offset))
        return int(v.bits & 1)

    def set(self, value):
        if self.settings.get("mode") != "output":
            raise GpioError("line %d is an input - set it to output first" % self.offset)
        v = LineValues(1 if value else 0, 1)
        try:
            fcntl.ioctl(self.fd, LINE_SET_VALUES, v)
        except OSError as e:
            raise _explain(e, "writing %s line %d" % (self.path, self.offset))

    def read_events(self):
        """Pending edge events: [(timestamp_ns, "rising"|"falling")]."""
        out = []
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return out
        for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            ts, ev_id, _off, _seq, _lseq = struct.unpack_from(EVENT_FMT, data, i)
            out.append((ts, "rising" if ev_id == EVENT_RISING else "falling"))
        return out

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
