"""Feature modules (MOD-01..04): which groups of features this server offers.

Every op belongs to exactly one module (by its prefix). A machine enables the modules it
needs in config.json (`"modules": [...]`, null = every available one); ops of a disabled
module answer "module X is not enabled", and the controllers hide its pages because hello
reports the enabled modules.

Modules only use standard Linux interfaces (NetworkManager, BlueZ, V4L2, the GPIO character
device, i2c-dev, spidev, termios, sysfs PWM/LED/IIO, X11). Board specifics - the Orange Pi
5 Plus header pin map, the Rockchip HDMI receiver - live in `boards/` and only add
information (MOD-03).
"""

import os
import shutil

# name -> (title, op prefixes, description)
MODULES = {
    "monitor": ("Monitor", ("stats.",), "CPU, memory, temperatures, disks, network, processes"),
    "system": ("System", ("admin.", "web.", "system.", "log."),
               "server info, logs, pairing, web access, restart, reboot"),
    "camera": ("Camera", ("recorder.", "gallery.", "jobs.", "camera."),
               "live view and recording of a camera or the HDMI input, gallery"),
    "connection": ("Connection", ("wifi.", "net.", "bt."), "Wi-Fi, Ethernet and Bluetooth devices"),
    "terminal": ("Terminal", ("term.",), "shared shells"),
    "screen": ("Screen", ("screen.", "in."), "the desktop, remote keyboard and mouse"),
    "io": ("IO Control", ("io.",), "GPIO, I2C, SPI, UART, PWM, LEDs, ADC"),
    "files": ("Files", ("files.",), "browse, upload and download files"),
    "apps": ("Apps", ("apps.",), "native apps with a web UI through the NTWB bridge"),
}
ALWAYS = ("system",)          # needed to manage the server itself

# Ops every session needs, whatever is enabled
CORE_OPS = ("hello", "ping", "state.get")


def available():
    """{module: reason-or-None}: None when the machine can run it."""
    out = {name: None for name in MODULES}
    if not shutil.which("nmcli"):
        out["connection"] = "NetworkManager (nmcli) is not installed"
    if not (os.environ.get("DISPLAY") or os.path.exists("/tmp/.X11-unix")):
        out["screen"] = "no X display"
    return out


def enabled(config):
    """Names of the enabled modules, in display order."""
    wanted = config.get("modules")
    if wanted is None or wanted == "all":
        names = list(MODULES)
    else:
        if isinstance(wanted, str):
            wanted = [w.strip() for w in wanted.split(",") if w.strip()]
        unknown = [w for w in wanted if w not in MODULES]
        if unknown:
            import logging
            logging.getLogger("arstro").warning("unknown modules in config ignored: %s", ", ".join(unknown))
        names = [n for n in MODULES if n in wanted or n in ALWAYS]
    # the old switches still work
    if not config.get("recorder_enabled", True) and "camera" in names:
        names.remove("camera")
    if not config.get("screen_enabled", True) and "screen" in names:
        names.remove("screen")
    return names


def module_of(op):
    for name, (_title, prefixes, _desc) in MODULES.items():
        if op.startswith(prefixes):
            return name
    return None


def describe(names, services=None):
    """For hello / system.modules: every module with its state."""
    avail = available()
    out = []
    for name, (title, prefixes, desc) in MODULES.items():
        state = "enabled" if name in names else "disabled"
        why = None
        if name in names and services is not None and services.get(name) is False:
            state, why = "failed", "did not start (see the server log)"
        elif name in names and avail.get(name):
            why = avail[name]
        out.append({"name": name, "title": title, "description": desc, "state": state,
                    "note": why, "ops": [p.rstrip(".") for p in prefixes]})
    return out
