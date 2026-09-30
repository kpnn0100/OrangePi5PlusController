"""A throwaway Arstro Remote instance for tests: slot "t", temporary config/state/data/
runtime dirs under /tmp (short, for Unix socket paths), a free port, no Bluetooth.

    t = Instance(modules=["system", "apps"], config={"apps_registered": [...]})
    t.start()         # t.c = a hello'd local-socket Client, t.base = http://127.0.0.1:<port>
    ...
    t.stop()          # stops the server, removes every directory
"""

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from harness import wait_until

from arstro_remote.client import Client

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Instance:
    def __init__(self, modules, config=None, app="test"):
        self.dir = tempfile.mkdtemp(prefix="arstro-test-")
        self.port = free_port()
        self.token = secrets.token_urlsafe(12)
        self.files = os.path.join(self.dir, "files")
        os.makedirs(self.files)
        self.env = dict(os.environ, ARSTRO_SLOT="t", XDG_CONFIG_HOME=self.dir + "/cfg", XDG_STATE_HOME=self.dir + "/state",
                        XDG_DATA_HOME=self.dir + "/data", XDG_RUNTIME_DIR=self.dir + "/run", PYTHONPATH=SERVER)
        os.makedirs(self.dir + "/run", mode=0o700)
        cfg = os.path.join(self.dir, "cfg", "arstro-remote-t")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "config.json"), "w") as f:
            cfg_data = {"web_port": self.port, "web_host": "127.0.0.1", "bluetooth_enabled": False, "modules": modules,
                        "files_roots": [self.files], "log_level": "info"}
            cfg_data.update(config or {})
            json.dump(cfg_data, f)
        fd = os.open(os.path.join(cfg, "web_token"), os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, (self.token + "\n").encode())
        os.close(fd)
        self.log = os.path.join(self.dir, "state", "arstro-remote-t", "arstro-remote.log")
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.app_name = app

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "arstro_remote", "run"], cwd=SERVER, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sock = os.path.join(self.dir, "run", "arstro-remote-t.sock")
        if not wait_until(lambda: os.path.exists(sock) and self._ping(), 20):
            raise RuntimeError("test instance did not start (see %s)" % self.log)
        self.c = Client.unix(sock)
        self.hello = self.c.call("hello", app=self.app_name)

    def _ping(self):
        try:
            urllib.request.urlopen(self.base + "/api/ping", timeout=1).read()
            return True
        except OSError:
            return False

    def http(self, method, path, data=None, headers=None):
        h = {"Authorization": "Bearer " + self.token}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stop(self):
        try:
            self.c.close()
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)
