"""Paths and configuration, without heavy imports (the remote CLI uses this too).

Slots (A/B, like A/B boot partitions): several server instances can be installed side by
side, each with its own code, config, logs, socket and port, so a new version is installed
and tested in one slot while the other keeps serving (ADM-06). The slot comes from
`ARSTRO_SLOT` (set by the launcher / `arstro-remote-<slot>` wrapper). Without it the
instance is the plain, unslotted `arstro-remote` (development, tests, old installs).
"""

import json
import os
import re
import sys

DEFAULT_CONFIG = {
    # Bluetooth name shown to phones. {hostname} is replaced. null keeps the current name.
    "alias": "Arstro-{hostname}",
    # The app's Bluetooth link (RFCOMM profile). Only one instance per machine can own it.
    "bluetooth_enabled": True,
    # "window": pairing allowed for pair_window_sec after start (and after `arstro-remote pair`)
    # "always": always discoverable and pairable, "never": only already-paired phones
    "pairing": "window",
    "pair_window_sec": 600,
    # Preferred RFCOMM channel (a free one is chosen if it is taken)
    "channel": 22,
    # Adapter name (e.g. "hci0"); null = first adapter
    "adapter": None,
    # How long a shell nobody views survives (dropped links, closed web pages)
    "term_keep_sec": 600,
    # Web server: web page, remote CLI and the app's media link (preview, gallery)
    "web_enabled": True,
    "web_host": "0.0.0.0",
    "web_port": 8080,
    # Feature modules (MOD-01): null = every module this machine supports, or a list such as
    # ["monitor", "system", "terminal", "files"]. See modules.py for the names.
    "modules": None,
    # Camera: recorder_simulate = "1920x1080@30" uses a test pattern (REC-08),
    # camera_device = "/dev/videoN" a V4L2 camera instead of the board's HDMI input (CAM-02)
    "recorder_enabled": True,
    "screen_enabled": True,
    "recorder_simulate": None,
    "camera_device": None,
    # Files module (FILE-01): folders a controller may browse, upload to and download from
    "files_roots": ["~"],
    # Apps (APP-01): NTWB manifests registered by path, besides the installed ones
    "apps_registered": [],
    # Logging (LOG-01): "debug" | "info" | "warning"
    "log_level": "info",
    # Poll Wi-Fi status this often so changes made outside the server are pushed too
    "wifi_poll_sec": 10,
}

SLOT_RE = re.compile(r"^[a-z]$")


def slot():
    """This instance's slot ("a", "b", ...) or None."""
    s = (os.environ.get("ARSTRO_SLOT") or "").strip().lower()
    if not s:
        return None
    if not SLOT_RE.match(s):
        raise ValueError("ARSTRO_SLOT must be one letter (a, b, ...), not %r" % s)
    return s


def instance_name(s=None):
    s = slot() if s is None else s
    return "arstro-remote-%s" % s if s else "arstro-remote"


def default_port(s=None):
    """8080 for slot A (and unslotted), 8081 for B, ..."""
    s = slot() if s is None else s
    return 8080 + (ord(s) - ord("a") if s else 0)


def _xdg(var, fallback):
    return os.environ.get(var) or os.path.expanduser(fallback)


def data_dir():
    """Data shared by every slot (the Android app offered for download)."""
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "arstro-remote")


def install_dir(s=None):
    """Where this slot's code is installed."""
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), instance_name(s))


def app_apk():
    """(path, version) of the Android app the Pi offers for download (SET-04), or (None, None)."""
    d = os.path.join(data_dir(), "app")
    path = os.path.join(d, "arstro-remote.apk")
    if not os.path.isfile(path):
        return None, None
    try:
        with open(os.path.join(d, "version")) as f:
            version = f.read().strip() or None
    except OSError:
        version = None
    return path, version


def config_dir(s=None):
    return os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), instance_name(s))


def state_dir(s=None):
    """Logs and runtime state of this instance (LOG-01)."""
    return os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), instance_name(s))


def cache_dir():
    return os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), "arstro-remote")


def runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


def control_socket_path(s=None):
    return os.environ.get("ARSTRO_SOCKET") or os.path.join(runtime_dir(), instance_name(s) + ".sock")


def lock_path(s=None):
    return os.path.join(runtime_dir(), instance_name(s) + ".lock")


def load_config(s=None):
    cfg = dict(DEFAULT_CONFIG)
    cfg["web_port"] = default_port(s)
    path = os.path.join(config_dir(s), "config.json")
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print("arstro-remote: ignoring bad config %s: %s" % (path, e), file=sys.stderr)
    return cfg


def save_config_values(values, s=None):
    """Merge `values` into config.json (keeps keys the user wrote by hand)."""
    path = os.path.join(config_dir(s), "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.update(values)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return cfg
