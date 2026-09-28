"""Web / remote-CLI access (SEC-03).

The access password lives in ~/.config/arstro-remote/web_token (mode 0600). It is random
until the user sets their own (`arstro-remote web --set-password`, or System in any
controller). The app gets it over Bluetooth (web.info), the CLI on the Pi reads the file,
a browser asks for it once and then keeps a cookie derived from it (so the cookie never
holds the password and changing the password logs every browser out).

Open mode (file `web_open` present) switches the password off entirely.
"""

import hashlib
import hmac
import os
import secrets
import threading

_lock = threading.Lock()
MIN_LEN = 8
MAX_LEN = 256


def token_path():
    from ..paths import config_dir
    return os.path.join(config_dir(), "web_token")


def _open_flag():
    from ..paths import config_dir
    return os.path.join(config_dir(), "web_open")


def _write(token):
    path = token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, path)


def load():
    """Current password (a random one is created on first use). None if unreadable."""
    with _lock:
        try:
            with open(token_path()) as f:
                tok = f.read().rstrip("\n")
            if len(tok) >= MIN_LEN:
                return tok
        except FileNotFoundError:
            pass
        except OSError:
            return None
        tok = secrets.token_urlsafe(24)
        _write(tok)
        return tok


def rotate():
    """Replace the password with a random one."""
    with _lock:
        tok = secrets.token_urlsafe(24)
        _write(tok)
        return tok


def set_password(value):
    value = str(value or "")
    if not MIN_LEN <= len(value) <= MAX_LEN:
        raise ValueError("the password needs %d to %d characters" % (MIN_LEN, MAX_LEN))
    if value != value.strip() or "\n" in value:
        raise ValueError("the password cannot start or end with spaces or contain line breaks")
    with _lock:
        _write(value)
    return value


def is_open():
    return os.path.exists(_open_flag())


def set_open(on):
    path = _open_flag()
    if on:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("web access without a password (arstro-remote web --require-password to undo)\n")
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def check(candidate, current=None):
    current = current or load()
    if not candidate or not current:
        return False
    return hmac.compare_digest(str(candidate).encode(), current.encode())


def cookie_value(current=None):
    """What the browser keeps: derived from the password, useless anywhere else."""
    current = current or load() or ""
    return hmac.new(current.encode(), b"arstro-web-cookie-v1", hashlib.sha256).hexdigest()


def check_cookie(candidate, current=None):
    if not candidate:
        return False
    return hmac.compare_digest(str(candidate).encode(), cookie_value(current).encode())
