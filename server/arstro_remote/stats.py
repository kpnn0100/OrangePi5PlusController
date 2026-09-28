"""System statistics for the dashboard (Orange Pi 5 Plus / RK3588 aware)."""

import glob
import os
import platform
import socket
import subprocess
import threading
import time

import psutil

_DEVFREQ_NAMES = {"gpu": "fb000000.gpu", "npu": "fdab0000.npu", "dmc": "dmc"}


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _os_name():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _default_route():
    """Return (iface, gateway) of the default IPv4 route, or (None, None)."""
    try:
        with open("/proc/net/route") as f:
            next(f)
            best = None
            for line in f:
                parts = line.split()
                if len(parts) < 8 or parts[1] != "00000000":
                    continue
                metric = int(parts[6])
                gw = socket.inet_ntoa(int(parts[2], 16).to_bytes(4, "little"))
                if best is None or metric < best[2]:
                    best = (parts[0], gw, metric)
            if best:
                return best[0], best[1]
    except (OSError, StopIteration, ValueError):
        pass
    return None, None


def temperatures():
    """All thermal zones in degrees C, keyed by a short name."""
    temps = {}
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        name = _read(zone + "/type")
        raw = _read(zone + "/temp")
        if not name or raw is None:
            continue
        try:
            value = int(raw) / 1000.0
        except ValueError:
            continue
        name = name.replace("-thermal", "").replace("_thermal", "")
        temps[name] = round(value, 1)
    return temps


def _devfreq():
    out = {}
    for key, node in _DEVFREQ_NAMES.items():
        load = _read("/sys/class/devfreq/%s/load" % node)
        if not load:
            continue
        try:
            pct, freq = load.split("@")
            out[key] = {"load": int(pct), "mhz": int(freq.rstrip("Hz")) // 1_000_000}
        except ValueError:
            continue
    return out


def _fan():
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(hw + "/name") == "pwmfan":
            pwm = _read(hw + "/pwm1")
            if pwm is not None and pwm.isdigit():
                return round(int(pwm) * 100 / 255)
    return None


class StatsCollector:
    """Keeps counters between samples so rates (CPU %, net B/s) are meaningful."""

    WIFI_TTL = 10.0

    def __init__(self):
        self._lock = threading.Lock()
        self._last_net = None
        self._last_net_t = None
        self._wifi_cache = (0.0, None)
        self._cache = (0.0, None)
        psutil.cpu_percent(percpu=True)  # prime the counters

    def _wifi(self):
        ts, value = self._wifi_cache
        if value is not None and time.monotonic() - ts < self.WIFI_TTL:
            return value
        value = {}
        try:
            out = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,DEVICE", "dev", "wifi", "list", "--rescan", "no"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            from .wifi import split_terse
            for line in out.splitlines():
                f = split_terse(line)
                if len(f) >= 4 and f[0] == "yes":
                    value = {"ssid": f[1], "signal": int(f[2] or 0), "device": f[3]}
                    break
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        self._wifi_cache = (time.monotonic(), value)
        return value

    def _network(self):
        now = time.monotonic()
        counters = psutil.net_io_counters(pernic=True)
        addrs = psutil.net_if_addrs()
        if_stats = psutil.net_if_stats()
        prev, prev_t = self._last_net, self._last_net_t
        self._last_net, self._last_net_t = counters, now
        dt = (now - prev_t) if prev_t else None
        ifaces = []
        for name, entries in sorted(addrs.items()):
            if name == "lo" or name.startswith(("p2p-dev", "docker", "veth", "br-")):
                continue
            ipv4 = [a.address for a in entries if a.family == socket.AF_INET]
            ipv6 = [a.address.split("%")[0] for a in entries
                    if a.family == socket.AF_INET6 and not a.address.startswith("fe80")]
            mac = next((a.address for a in entries if a.family == psutil.AF_LINK), None)
            st = if_stats.get(name)
            c = counters.get(name)
            rx_rate = tx_rate = 0
            if c and prev and dt and name in prev:
                rx_rate = max(0, int((c.bytes_recv - prev[name].bytes_recv) / dt))
                tx_rate = max(0, int((c.bytes_sent - prev[name].bytes_sent) / dt))
            ifaces.append({
                "name": name,
                "up": bool(st and st.isup),
                "ipv4": ipv4,
                "ipv6": ipv6,
                "mac": mac,
                "speed": st.speed if st else 0,
                "rx": c.bytes_recv if c else 0,
                "tx": c.bytes_sent if c else 0,
                "rx_rate": rx_rate,
                "tx_rate": tx_rate,
            })
        gw_iface, gw = _default_route()
        primary_ip = None
        for i in ifaces:
            if i["name"] == gw_iface and i["ipv4"]:
                primary_ip = i["ipv4"][0]
        if primary_ip is None:
            primary_ip = next((i["ipv4"][0] for i in ifaces if i["ipv4"]), None)
        return {"interfaces": ifaces, "gateway": gw, "gateway_iface": gw_iface, "ip": primary_ip}

    def _top_processes(self, n=6):
        procs = []
        for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent"]):
            info = p.info
            if info["cpu_percent"] is None:
                continue
            procs.append(info)
        procs.sort(key=lambda i: (i["cpu_percent"] or 0, i["memory_percent"] or 0), reverse=True)
        return [{
            "pid": i["pid"],
            "name": i["name"],
            "user": i["username"],
            "cpu": round(i["cpu_percent"] or 0, 1),
            "mem": round(i["memory_percent"] or 0, 1),
        } for i in procs[:n]]

    def collect(self, max_age=0.8):
        """Return a full snapshot. Snapshots younger than max_age are reused so
        several subscribers do not skew the rate calculations."""
        with self._lock:
            ts, cached = self._cache
            if cached is not None and time.monotonic() - ts < max_age:
                return cached
            snap = self._collect()
            self._cache = (time.monotonic(), snap)
            return snap

    def _collect(self):
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        per_cpu = psutil.cpu_percent(percpu=True)
        freqs = []
        try:
            freqs = [int(f.current) for f in psutil.cpu_freq(percpu=True)]
        except (OSError, AttributeError, TypeError):
            pass
        disks = []
        for part in psutil.disk_partitions():
            if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            disks.append({"mount": part.mountpoint, "device": part.device, "fstype": part.fstype,
                          "total": u.total, "used": u.used, "free": u.free, "percent": u.percent})
        temps = temperatures()
        cpu_temp = max((v for k, v in temps.items()
                        if k in ("soc", "bigcore0", "bigcore1", "littlecore", "center")), default=None)
        load1, load5, load15 = os.getloadavg()
        return {
            "time": int(time.time()),
            "hostname": socket.gethostname(),
            "os": _os_name(),
            "kernel": platform.release(),
            "arch": platform.machine(),
            "uptime": int(time.time() - psutil.boot_time()),
            "cpu": {
                "percent": round(sum(per_cpu) / len(per_cpu), 1) if per_cpu else 0,
                "per_core": [round(p, 1) for p in per_cpu],
                "freq_mhz": freqs,
                "count": psutil.cpu_count(),
                "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
            },
            "memory": {"total": vm.total, "used": vm.total - vm.available,
                       "available": vm.available, "percent": vm.percent},
            "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
            "temps": temps,
            "cpu_temp": cpu_temp,
            "devfreq": _devfreq(),
            "fan_percent": _fan(),
            "disks": disks,
            "network": self._network(),
            "wifi": self._wifi(),
            "processes": self._top_processes(),
        }
