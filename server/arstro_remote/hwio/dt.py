"""Device-tree names for hardware controllers (standard /proc/device-tree, no board code).

A controller such as /sys/class/pwm/pwmchip0 points at its firmware node
(`device/of_node` -> /sys/firmware/devicetree/base/pwm@fd8b0020). The DT `__symbols__`
(present when the tree was built for overlays) and `aliases` give it the name the
board documentation uses: "pwm2", "i2c2", "serial9"...
"""

import functools
import os

DT = "/proc/device-tree"
FW_BASE = "/sys/firmware/devicetree/base"


def _read_str(path):
    try:
        with open(path, "rb") as f:
            return f.read().rstrip(b"\0").decode("utf-8", "replace")
    except OSError:
        return None


@functools.lru_cache(maxsize=1)
def _names():
    """{node path: [names]} from __symbols__ and aliases."""
    out = {}
    for sub in ("aliases", "__symbols__"):
        d = os.path.join(DT, sub)
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for name in entries:
            if name in ("name", "phandle"):
                continue
            path = _read_str(os.path.join(d, name))
            if path and path.startswith("/"):
                out.setdefault(path, [])
                if name not in out[path]:
                    out[path].append(name)
    return out


def node_of(sysfs_dev):
    """DT node path ("/pwm@fd8b0020") of a sysfs device, or None."""
    for rel in ("of_node", "device/of_node"):
        p = os.path.join(sysfs_dev, rel)
        if os.path.exists(p):
            real = os.path.realpath(p)
            if real.startswith(FW_BASE):
                return real[len(FW_BASE):] or "/"
    return None


def names_of(sysfs_dev, prefix=None):
    """DT names of a sysfs device, e.g. ["i2c2"]; filtered by `prefix` when given."""
    node = node_of(sysfs_dev)
    if not node:
        return []
    names = _names().get(node, [])
    if prefix:
        names = [n for n in names if n.startswith(prefix)]
    return names


def model():
    return _read_str(os.path.join(DT, "model"))


def compatible():
    s = None
    try:
        with open(os.path.join(DT, "compatible"), "rb") as f:
            s = f.read()
    except OSError:
        return []
    return [c.decode("ascii", "replace") for c in s.split(b"\0") if c]
