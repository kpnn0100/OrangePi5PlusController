#!/usr/bin/env python3
"""Why did the board / the server go away? Reads the evidence a reset leaves and prints it.

    python3 .claude/skills/arstro.remote.server.debug/diagnose.py [--boots N] [--smart]

Sources (see SKILL.md for why each one):
  ~/sysmon/health.log   one fsync'd line every 5 s (temps, clocks, load, memory, power, devices)
  ~/sysmon/kernel.log*  every kernel message, fsync'd
  NVMe SMART            power cycles / unsafe shutdowns / temperature time (--smart: needs sudo;
                        the password is read from $SUDO_PASS or asked - never stored)
  sysfs now             every hwmon by name, the USB-C power contract, PCIe devices
Each SMART reading is appended to ~/sysmon/nvme-counters.log, so the next run can tell how many
power losses the DRIVE saw against how many times the BOARD booted in between.
"""
import argparse
import datetime
import glob
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
SYSMON = os.path.join(HOME, "sysmon")
HEALTH = os.path.join(SYSMON, "health.log")
COUNTERS = os.path.join(SYSMON, "nvme-counters.log")
LINE = re.compile(r"^(\S+ \S+) up=(\d+)s load=\[([\d. ]+)\].*?temp\[([^\]]*)\] mhz\[([^\]]*)\]")
KERNEL_BAD = re.compile(r"nvme|pcie|aer:|I/O error|EXT4-fs (error|warning)|blk_update|iwlwifi.*(error|fail|CT.?kill|"
                        r"Hardware became|restart|microcode|timeout)|thermal.*(crit|hot)|over.?current|"
                        r"under.?volt|Kernel panic|Oops|BUG:|watchdog|soft lockup|hung_task|Out of memory", re.I)
KERNEL_NOISE = re.compile(r"hdmirx|rkvenc|rkvdec|mali|Unknown advertising|reset resource|signal-change", re.I)


def sh(cmd, inp=None):
    try:
        return subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def boots():
    """[(boot_id, marker_line, [lines])] - a sysmon RESTART writes a marker with the same boot id,
    so markers are merged by id: only a new id is a new boot."""
    out = []
    if not os.path.exists(HEALTH):
        return out
    for line in open(HEALTH, errors="replace"):
        m = re.match(r"^=== BOOT (\S+) at", line)
        if m:
            if out and out[-1][0] == m.group(1):
                continue
            out.append((m.group(1), line.strip(), []))
        elif out:
            out[-1][2].append(line.rstrip())
    return out


def parse(line):
    m = LINE.match(line)
    if not m:
        return None
    temps = dict(kv.split("=") for kv in m.group(4).split() if "=" in kv)
    return {"t": m.group(1), "up": int(m.group(2)), "load": float(m.group(3).split()[0]),
            "tmax": max((int(v) for v in temps.values()), default=0), "temps": temps,
            "mhz": [int(x) for x in m.group(5).split()], "raw": line}


def report_boots(n):
    bs = boots()
    print(f"== {len(bs)} boots in {HEALTH}; the last {min(n, len(bs))} (state in the last 5 s before each ended)")
    print("   NOTE: no RTC - a boot's clock resumes from the last saved time, so wall times jump; use up=")
    burst = 0
    for bid, marker, lines in bs[-n:]:
        ps = [p for p in map(parse, lines) if p]
        if not ps:
            continue
        last, peak = ps[-1], max(ps, key=lambda p: p["tmax"])
        stalls = [(a["t"], b["up"] - a["up"]) for a, b in zip(ps, ps[1:]) if b["up"] - a["up"] > 20]
        maxed = max(last["mhz"]) >= 2200
        burst += maxed
        print(f"  {bid[:8]} ran {last['up']:>6}s  end: load {last['load']:<5} cores {last['mhz']} "
              f"temp {last['tmax']}C (peak {peak['tmax']}C){'  BIG CORES AT MAX' if maxed else ''}"
              f"{'  STALLS ' + str(stalls[:3]) if stalls else ''}")
        i = last["raw"].find(" pd=")
        if i >= 0:
            print(f"           {last['raw'][i + 1:][:110]}")
    if bs:
        print(f"   {burst} of the last {min(n, len(bs))} runs ended with the big cores at their top clock")


def report_kernel():
    seen, hits = set(), []
    for f in (os.path.join(SYSMON, "kernel.log.1"), os.path.join(SYSMON, "kernel.log")):
        if not os.path.exists(f):
            continue
        for line in open(f, errors="replace"):
            if KERNEL_BAD.search(line) and not KERNEL_NOISE.search(line):
                key = re.sub(r"^\S+ \S+ kernel: ", "", line.strip())[:150]
                if key not in seen:
                    seen.add(key)
                    hits.append(line.strip()[:200])
    print(f"== kernel: {len(hits)} distinct storage / PCIe / Wi-Fi / thermal / lockup lines")
    for h in hits[-25:]:
        print("  " + h)
    if not hits:
        print("  none - and remember the log lives ON the NVMe: a dying NVMe cannot write its own obituary")


def report_now(smart):
    print("== now")
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            name = open(h + "/name").read().strip()
            t = int(open(h + "/temp1_input").read()) / 1000
            print(f"  {name:<24} {t:5.1f} C")
        except OSError:
            pass
    mode = sh(["sh", "-c", "cat /sys/class/typec/port0/power_operation_mode 2>/dev/null"]).strip()
    online = sh(["sh", "-c", "cat /sys/class/power_supply/tcpm-source-psy-*/online 2>/dev/null"]).strip()
    print(f"  USB-C power: operation mode {mode or '?'}, PD contract online={online or '?'}"
          f"{'   <- NO PD CONTRACT: the supply only promises default USB current' if online in ('0', '') else ''}")
    print("  PCIe: " + "; ".join(l.split(": ", 1)[-1][:40] for l in sh(["lspci"]).splitlines() if "bridge" not in l))
    print(f"  nvme0 state: {sh(['sh', '-c', 'cat /sys/class/nvme/nvme0/state 2>/dev/null']).strip() or 'GONE'}")
    if not smart:
        print("  (NVMe SMART skipped - add --smart)")
        return
    pw = os.environ.get("SUDO_PASS")
    if pw is None and sys.stdin.isatty():
        import getpass
        pw = getpass.getpass("sudo password (for smartctl; not stored): ")
    out = sh(["sudo", "-S", "-p", "", "smartctl", "-A", "/dev/nvme0"], (pw or "") + "\n")
    want = ("Temperature:", "Power Cycles", "Power On Hours", "Unsafe Shutdowns", "Media and Data",
            "Warning  Comp. Temperature Time", "Critical Comp. Temperature Time", "Critical Warning")
    vals = {}
    for l in out.splitlines():
        for w in want:
            if l.startswith(w):
                print("  " + re.sub(r"\s+", " ", l))
                vals[w] = re.sub(r"[^\d]", "", l.split(":", 1)[1].split()[0]) if ":" in l else ""
    if not vals:
        print("  smartctl gave nothing (sudo refused or smartmontools missing)")
        return
    nboots = len(boots())
    stamp = datetime.datetime.now().strftime("%F %T")
    prev = open(COUNTERS).read().splitlines()[-1] if os.path.exists(COUNTERS) else None
    rec = f"{stamp} boots={nboots} cycles={vals.get('Power Cycles', '?')} unsafe={vals.get('Unsafe Shutdowns', '?')} poh={vals.get('Power On Hours', '?')}"
    with open(COUNTERS, "a") as f:
        f.write(rec + "\n")
    if prev:
        p = dict(kv.split("=") for kv in prev.split()[2:])
        c = dict(kv.split("=") for kv in rec.split()[2:])
        try:
            db, dc = int(c["boots"]) - int(p["boots"]), int(c["cycles"]) - int(p["cycles"])
            print(f"  since {prev[:19]}: board booted {db}x, the DRIVE lost power {dc}x"
                  f"{'   <- the drive loses power WITHOUT the board rebooting: M.2 rail dips' if dc > db + 1 else ''}")
        except (KeyError, ValueError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boots", type=int, default=20)
    ap.add_argument("--smart", action="store_true")
    a = ap.parse_args()
    report_boots(a.boots)
    report_kernel()
    report_now(a.smart)


if __name__ == "__main__":
    main()
