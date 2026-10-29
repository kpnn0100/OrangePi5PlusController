#!/usr/bin/env python3
"""An nginx 1.14 in Docker in front of a local server, set up like a reverse proxy that does not
pass WebSockets (docs/relay/PROTOCOL.md section 6) - for tests of the HTTP transport.

    sys.path.insert(0, "<repo>/relay/tests")
    from nginx_proxy import NginxProxy, NginxUnavailable

    try:
        with NginxProxy(relay_port) as url:     # url = "http://127.0.0.1:<port>" (no trailing /)
            ...                                 # url + "/arstro/internal/devices/..." reaches 127.0.0.1:<relay_port>
    except NginxUnavailable as e:               # no docker / no image / nginx did not start
        print("SKIP:", e)

The proxy: `location / { proxy_pass http://127.0.0.1:<relay_port>; }` with nginx's defaults -
HTTP/1.0 to the server without the Upgrade / Connection headers (a WebSocket handshake reaches
the server as a plain GET, the relay answers 426), response buffering on, `proxy_read_timeout
60s`, `client_max_body_size 1m` - plus `Host $http_host`, `X-Forwarded-For` and
`X-Forwarded-Proto` (so Origin checks pass for a browser that opens `url`; set
RELAY_TRUST_PROXY=1 on the relay to see the client address). `NginxProxy(port, forward_host=False)`
leaves the Host header at nginx's default (the upstream address); `extra="..."` adds lines to the
location block.

The container (`docker run --rm --network host`, name `arstro-test-nginx-<pid>-<random>`) shares
this host's network: nginx listens on 127.0.0.1:<a free port> and reaches a server that listens
on 127.0.0.1. Linux only (Docker Desktop has no host network). The image `nginx:1.14` is pulled
when missing. The container is removed when the `with` block ends (or by `stop()`).

    python3 relay/tests/nginx_proxy.py <server port>     # run one until Ctrl-C (manual tests)
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

IMAGE = "nginx:1.14"

CONFIG = """worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events { worker_connections 512; }
http {
    access_log /dev/stdout;
    client_body_temp_path /tmp/client_body;
    proxy_temp_path /tmp/proxy;
    server {
        listen 127.0.0.1:%(port)d;
        client_max_body_size 1m;
        location / {
            proxy_pass http://127.0.0.1:%(upstream)d;
            proxy_read_timeout 60s;
%(headers)s%(extra)s
        }
    }
}
"""
HEADERS = """            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
"""


class NginxUnavailable(RuntimeError):
    """Docker or the nginx image cannot be used here (the reason is the message)."""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _docker(*args, timeout=60):
    try:
        return subprocess.run(["docker"] + list(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise NginxUnavailable("docker is not installed")
    except subprocess.TimeoutExpired:
        raise NginxUnavailable("docker %s did not finish in %d s" % (args[0], timeout))


class NginxProxy:
    """nginx 1.14 proxying http://127.0.0.1:<port> to 127.0.0.1:<upstream_port> (see the module docstring)."""

    def __init__(self, upstream_port, forward_host=True, extra=""):
        self.upstream = upstream_port
        self.forward_host, self.extra = forward_host, extra
        self.port = None
        self.url = None
        self.name = "arstro-test-nginx-%d-%s" % (os.getpid(), secrets.token_hex(3))
        self._dir = None

    def start(self):
        """Start the container and wait until nginx answers -> the proxy's URL. NginxUnavailable if it cannot."""
        if _docker("version", "--format", "{{.Server.Version}}", timeout=20).returncode != 0:
            raise NginxUnavailable("the docker daemon is not reachable")
        if _docker("image", "inspect", IMAGE, timeout=20).returncode != 0:
            r = _docker("pull", IMAGE, timeout=600)
            if r.returncode != 0:
                raise NginxUnavailable("cannot pull %s: %s" % (IMAGE, r.stderr.strip()[-300:]))
        self.port = _free_port()
        self._dir = tempfile.mkdtemp(prefix="nginx-proxy-")
        conf = os.path.join(self._dir, "nginx.conf")
        with open(conf, "w") as f:
            f.write(CONFIG % {"port": self.port, "upstream": self.upstream,
                              "headers": HEADERS if self.forward_host else "",
                              "extra": "".join("            %s\n" % line.strip() for line in self.extra.splitlines()
                                               if line.strip())})
        os.chmod(self._dir, 0o755)
        os.chmod(conf, 0o644)
        r = _docker("run", "-d", "--rm", "--name", self.name, "--network", "host",
                    "-v", "%s:/etc/nginx/nginx.conf:ro" % conf, IMAGE)
        if r.returncode != 0:
            self._cleanup()
            raise NginxUnavailable("docker run failed: %s" % r.stderr.strip()[-300:])
        self.url = "http://127.0.0.1:%d" % self.port
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._answers():
                return self.url
            if _docker("inspect", "-f", "{{.State.Running}}", self.name, timeout=20).stdout.strip() != "true":
                break
            time.sleep(0.2)
        logs = self.logs()
        self.stop()
        raise NginxUnavailable("nginx did not start: %s" % logs.strip()[-500:])

    def _answers(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as s:
                s.sendall(b"GET /nginx-proxy-probe HTTP/1.0\r\nHost: probe\r\n\r\n")
                return s.recv(16).startswith(b"HTTP/")
        except OSError:
            return False

    def logs(self):
        """nginx's access and error log (docker logs)."""
        r = _docker("logs", self.name, timeout=20)
        return r.stdout + r.stderr

    def stop(self):
        """Remove the container (and its config)."""
        _docker("rm", "-f", self.name, timeout=60)
        self._cleanup()

    def _cleanup(self):
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: python3 relay/tests/nginx_proxy.py <server port>", file=sys.stderr)
        sys.exit(2)
    with NginxProxy(int(sys.argv[1])) as url:
        print("nginx %s -> 127.0.0.1:%s at %s (Ctrl-C stops it)" % (IMAGE, sys.argv[1], url), flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
