"""sysfs devices: PWM (/sys/class/pwm, IO-06), LEDs (/sys/class/leds, IO-07) and ADC
channels (IIO, /sys/bus/iio, IO-08). All standard kernel ABIs."""

import errno
import glob
import os
import re

from . import dt

PWM = "/sys/class/pwm"
LEDS = "/sys/class/leds"
IIO = "/sys/bus/iio/devices"


class SysfsError(Exception):
    pass


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _write(path, value, what):
    try:
        with open(path, "w") as f:
            f.write(str(value))
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            raise SysfsError("%s: permission denied - run install.sh (group gpio), then log in again" % what)
        if e.errno == errno.EBUSY:
            raise SysfsError("%s: busy (used by a kernel driver)" % what)
        raise SysfsError("%s: %s" % (what, e.strerror or e))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------------------- PWM
def _chip_dir(chip):
    s = str(chip)
    d = os.path.join(PWM, s if s.startswith("pwmchip") else "pwmchip" + s)
    if not re.fullmatch(r"pwmchip\d+", os.path.basename(d)) or not os.path.isdir(d):
        raise SysfsError("no PWM chip %r" % chip)
    return d


def pwm_list():
    out = []
    for d in sorted(glob.glob(PWM + "/pwmchip*"), key=lambda p: int(p.rsplit("pwmchip", 1)[1])):
        chip = int(d.rsplit("pwmchip", 1)[1])
        npwm = _int(_read(d + "/npwm")) or 0
        channels = []
        for ch in range(npwm):
            cd = "%s/pwm%d" % (d, ch)
            if os.path.isdir(cd):
                channels.append({"channel": ch, "exported": True,
                                 "period_ns": _int(_read(cd + "/period")),
                                 "duty_ns": _int(_read(cd + "/duty_cycle")),
                                 "polarity": _read(cd + "/polarity"),
                                 "enabled": _read(cd + "/enable") == "1"})
            else:
                channels.append({"channel": ch, "exported": False})
        out.append({"chip": chip, "npwm": npwm, "dt": dt.names_of(d + "/device", "pwm"),
                    "node": dt.node_of(d + "/device"), "writable": os.access(d + "/export", os.W_OK),
                    "channels": channels})
    return out


def pwm_set(chip, channel, period_ns=None, duty_ns=None, polarity=None, enabled=None):
    d = _chip_dir(chip)
    ch = int(channel)
    npwm = _int(_read(d + "/npwm")) or 0
    if not 0 <= ch < npwm:
        raise SysfsError("pwmchip%s has channels 0..%d" % (chip, npwm - 1))
    cd = "%s/pwm%d" % (d, ch)
    if not os.path.isdir(cd):
        _write(d + "/export", ch, "exporting pwm%d" % ch)
        for _ in range(50):                 # udev fixes the new files' permissions
            if os.access(cd + "/enable", os.W_OK):
                break
            import time
            time.sleep(0.02)
    cur_period = _int(_read(cd + "/period")) or 0
    if period_ns is not None:
        period_ns = int(period_ns)
        if period_ns <= 0:
            raise SysfsError("period must be > 0 ns")
    if duty_ns is not None:
        duty_ns = int(duty_ns)
        if duty_ns < 0 or duty_ns > (period_ns or cur_period):
            raise SysfsError("duty cycle must be 0..period")
    # the kernel refuses duty > period at every step, so order the writes
    if period_ns is not None and duty_ns is not None:
        if period_ns < cur_period:
            _write(cd + "/duty_cycle", duty_ns, "duty cycle")
            _write(cd + "/period", period_ns, "period")
        else:
            _write(cd + "/period", period_ns, "period")
            _write(cd + "/duty_cycle", duty_ns, "duty cycle")
    elif period_ns is not None:
        _write(cd + "/period", period_ns, "period")
    elif duty_ns is not None:
        _write(cd + "/duty_cycle", duty_ns, "duty cycle")
    if polarity is not None:
        if polarity not in ("normal", "inversed"):
            raise SysfsError("polarity must be normal or inversed")
        if _read(cd + "/polarity") != polarity:
            was = _read(cd + "/enable") == "1"
            if was:
                _write(cd + "/enable", 0, "disable")
            _write(cd + "/polarity", polarity, "polarity")
            if was and enabled is None:
                enabled = True
    if enabled is not None:
        _write(cd + "/enable", 1 if enabled else 0, "enable")
    return next(c for c in pwm_list() if c["chip"] == int(os.path.basename(d)[7:]))["channels"][ch]


def pwm_unexport(chip, channel):
    d = _chip_dir(chip)
    if os.path.isdir("%s/pwm%d" % (d, int(channel))):
        _write(d + "/unexport", int(channel), "unexporting pwm%d" % int(channel))


# ------------------------------------------------------------------------- LEDs
def _led_dir(name):
    if not name or "/" in name or name.startswith("."):
        raise SysfsError("no LED %r" % name)
    d = os.path.join(LEDS, name)
    if not os.path.isdir(d):
        raise SysfsError("no LED %r" % name)
    return d


def leds():
    out = []
    for d in sorted(glob.glob(LEDS + "/*")):
        trig = _read(d + "/trigger", "") or ""
        triggers = [t.strip("[]") for t in trig.split()]
        active = next((t.strip("[]") for t in trig.split() if t.startswith("[")), "none")
        out.append({"name": os.path.basename(d), "brightness": _int(_read(d + "/brightness")),
                    "max": _int(_read(d + "/max_brightness")), "trigger": active, "triggers": triggers,
                    "writable": os.access(d + "/brightness", os.W_OK)})
    return out


def led_set(name, brightness=None, trigger=None):
    d = _led_dir(name)
    if trigger is not None:
        _write(d + "/trigger", trigger, "LED trigger")
    if brightness is not None:
        mx = _int(_read(d + "/max_brightness")) or 1
        b = max(0, min(int(brightness), mx))
        _write(d + "/brightness", b, "LED brightness")
    return next(l for l in leds() if l["name"] == name)


# -------------------------------------------------------------------------- ADC
def adc():
    """Every IIO voltage channel: raw value, scale (mV per step), millivolts."""
    out = []
    for d in sorted(glob.glob(IIO + "/iio:device*")):
        name = _read(d + "/name", "")
        chans = []
        shared_scale = _read(d + "/in_voltage_scale")
        for raw in sorted(glob.glob(d + "/in_voltage*_raw"), key=lambda p: _int(re.sub(r"\D", "", os.path.basename(p))) or 0):
            ch = os.path.basename(raw)[len("in_voltage"):-len("_raw")]
            v = _int(_read(raw))
            scale = _read("%s/in_voltage%s_scale" % (d, ch)) or shared_scale
            try:
                s = float(scale) if scale else None
            except ValueError:
                s = None
            chans.append({"channel": ch, "raw": v, "scale": s,
                          "mv": round(v * s, 2) if v is not None and s is not None else None})
        out.append({"device": os.path.basename(d), "name": name, "dt": dt.names_of(d, "saradc") or dt.names_of(d),
                    "channels": chans})
    return out
