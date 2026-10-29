"""The relay's data directory (RELAY_DATA, /data in the container).

    devices.json   {id: {name, secret_sha256, hostname, platform, version, slot, web, first_seen, last_seen}}
    join_token     the token devices join with (when RELAY_JOIN_TOKEN is not set)
    cookie_key     random key for the viewer login cookie (HMAC)

Files are written atomically and readable only by the relay's user. The server and the admin
CLI change devices.json under one lock (read - change - write), so neither loses the other's
change; the server notices the CLI's changes by the file's mtime.
"""

import contextlib
import fcntl
import json
import logging
import os
import secrets

log = logging.getLogger("relay.store")


class Store:
    def __init__(self, path):
        self.dir = path
        os.makedirs(path, exist_ok=True)
        self.devices_path = os.path.join(path, "devices.json")
        self.token_path = os.path.join(path, "join_token")
        self.key_path = os.path.join(path, "cookie_key")
        self._lock_path = os.path.join(path, ".lock")
        self.written = None

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _write(self, path, text):
        tmp = "%s.tmp%d" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @staticmethod
    def stamp(path):
        """Changes whenever the file is replaced or written (None: no file)."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size, st.st_ino

    # ------------------------------------------------------------------ devices
    def devices(self):
        try:
            with open(self.devices_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError as e:
            log.error("%s is damaged (%s) - starting with an empty device list", self.devices_path, e)
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def update(self, change):
        """change(devices) edits the dict in place and it is written back (not when change
        returns False). -> the dict as it is now."""
        with self.locked():
            devices = self.devices()
            if change(devices) is not False:
                self._write(self.devices_path, json.dumps(devices, indent=1, sort_keys=True) + "\n")
            self.written = self.stamp(self.devices_path)     # what this process last saw / wrote
            return devices

    # ------------------------------------------------------------- join token
    def join_token(self, create=True):
        try:
            with open(self.token_path) as f:
                token = f.read().strip()
            if token:
                return token
        except FileNotFoundError:
            pass
        if not create:
            return None
        with self.locked():          # another process may have made it meanwhile
            return self.join_token(create=False) or self._new_token()

    def rotate_token(self):
        with self.locked():
            return self._new_token()

    def _new_token(self):
        token = secrets.token_urlsafe(24)
        self._write(self.token_path, token + "\n")
        return token

    # ------------------------------------------------------------- cookie key
    def cookie_key(self):
        with self.locked():
            try:
                with open(self.key_path) as f:
                    key = bytes.fromhex(f.read().strip())
                if len(key) >= 32:
                    return key
            except (FileNotFoundError, ValueError):
                pass
            key = secrets.token_bytes(32)
            self._write(self.key_path, key.hex() + "\n")
            return key
