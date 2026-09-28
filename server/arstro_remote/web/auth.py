"""Access token for the web server and the remote CLI (SEC-03).

A random 192-bit token lives in ~/.config/arstro-remote/web_token (mode 0600). The app
gets it over Bluetooth (web.info), the CLI on the Pi reads the file, the web page asks
for it once and keeps it in an HttpOnly cookie. `rotate()` makes a new one.
"""

import hmac
import os
import secrets
import threading

_lock = threading.Lock()


def token_path():
    from ..paths import config_dir
    return os.path.join(config_dir(), "web_token")


def _write(token):
    path = token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, path)


def load():
    """Current token (created on first use). None if the file can't be read."""
    with _lock:
        try:
            with open(token_path()) as f:
                tok = f.read().strip()
            if len(tok) >= 16:
                return tok
        except FileNotFoundError:
            pass
        except OSError:
            return None
        tok = secrets.token_urlsafe(24)
        _write(tok)
        return tok


def rotate():
    with _lock:
        tok = secrets.token_urlsafe(24)
        _write(tok)
        return tok


def check(candidate, current=None):
    current = current or load()
    if not candidate or not current:
        return False
    return hmac.compare_digest(str(candidate).encode(), current.encode())
