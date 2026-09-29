"""Paths and configuration, without heavy imports (the remote CLI uses this too)."""

import json
import os
import sys

DEFAULT_CONFIG = {
    # Bluetooth name shown to phones. {hostname} is replaced. null keeps the current name.
    "alias": "Arstro-{hostname}",
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
    # HDMI RX recorder; recorder_simulate = "1920x1080@30" uses a test pattern (REC-08)
    "recorder_enabled": True,
    "screen_enabled": True,
    "recorder_simulate": None,
    # Poll Wi-Fi status this often so changes made outside the server are pushed too
    "wifi_poll_sec": 10,
}


def data_dir():
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "arstro-remote")


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


def config_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "arstro-remote")


def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "arstro-remote")


def cache_dir():
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "arstro-remote")


def runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


def control_socket_path():
    return os.environ.get("ARSTRO_SOCKET") or os.path.join(runtime_dir(), "arstro-remote.sock")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(config_dir(), "config.json")
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print("arstro-remote: ignoring bad config %s: %s" % (path, e), file=sys.stderr)
    return cfg
