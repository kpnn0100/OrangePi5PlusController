---
name: arstro.remote.server.debug
description: Debug why the Arstro Remote board / server went away - a hard reset, the web or Wi-Fi gone while Bluetooth still answers, "cannot create a shell", the app disconnecting. Reads the crash-safe health and kernel logs (~/sysmon), the NVMe's own SMART counters, the sensors and the USB-C power contract, separates heat from power from software, and says what to capture next time. Read-only on the system except the sysmon logger. Invoke for "why did the pi disconnect / reboot / hang", "is it overheating", "is the wifi / nvme broken", "the server died but bluetooth works", "/arstro.remote.server.debug".
---

# arstro.remote.server.debug

**Find what killed the board or the server from evidence, not from a guess.** The first suspect is
usually heat; on this board it has so far always been **power**. Prove which one before touching
anything.

## Run this first

```bash
SUDO_PASS=<ask the user / read it from them; never store it> \
  python3 .claude/skills/arstro.remote.server.debug/diagnose.py --boots 20 --smart
```

It prints: each recent boot's last 5 s (uptime, load, clocks, every temperature, the power contract,
which devices were alive), stalls inside a boot, every distinct storage / PCIe / Wi-Fi / thermal /
lockup / OOM kernel line, the sensors now, and the NVMe SMART counters - appending them to
`~/sysmon/nvme-counters.log`, so the next run says *how many times the drive lost power against how
many times the board booted*.

## The facts that make this board hard to read (each cost time)

| fact | consequence |
|---|---|
| **No RTC.** The clock resumes from the last saved time at boot. | `journalctl --list-boots` interleaves boots and shows impossible ranges; wall-clock times jump. Use `up=` and boot ids. |
| **The only disk is the NVMe** (`mtdblock0` is the 16 MB bootloader flash, no eMMC/SD). | Every log - journal, `~/sysmon`, the server's - lives on it. **If the NVMe drops, nothing records the moment.** The logs just stop. |
| **NVMe, Wi-Fi (Intel AX200) and both Ethernet ports are all PCIe**; the AX200's **Bluetooth is USB**. | A PCIe / M.2-rail failure takes out disk + Wi-Fi + Ethernet *together* while Bluetooth and the running server (already in RAM) keep going: the app stays connected over Bluetooth but nothing new can be started from disk - no shell, no web. That symptom is the PCIe side dying, not the server. |
| **Power is USB-C** through a tcpm/husb311 controller: `/sys/class/power_supply/tcpm-source-psy-*/online`, `/sys/class/typec/port0/power_operation_mode`. | `online=0` + mode `default` = no PD contract: the supply only promises *default* USB current. The board wants 5 V / 4 A; an NVMe write burst + Wi-Fi TX + all big cores at 2.3 GHz is exactly when it pulls the most. |
| `~/sysmon/sysmon.sh` (user unit `sysmon.service`) writes fsync'd lines every 5 s. | The last line before a `=== BOOT` marker is the state ≤5 s before death. A restart of the logger writes a marker with the SAME boot id - only a new id is a reboot (the script merges them). |
| The NVMe counts its own **Power Cycles** and **Unsafe Shutdowns** (SMART, needs sudo). | The most honest power witness on the board: it counts losses the SoC survived, which no log can. |

## Reading the evidence

| you see | it means |
|---|---|
| Temps at death well under 85 °C (SoC throttles at 85, trips ~110); NVMe "Warning/Critical Comp. Temperature Time" = 0; `iwlwifi_1` ≈ 40-60 °C and no `CT kill` line | **not heat** |
| Deaths 30-160 s after boot or at a load spike, big cores at 2304 MHz, logs ending mid-stream with no kernel error | **brown-out**: the supply sags under a current step |
| NVMe Power Cycles rising faster than board boots (`nvme-counters.log` delta) | the **M.2 rail** dips while the SoC lives - the "Wi-Fi gone, Bluetooth alive, no shell" state |
| `dev[nvme=GONE ...]` or Wi-Fi `down` in health.log before the end (only visible if the log reached a USB stick, see below) | PCIe device dropped first - confirms the above |
| stalls (`up=` jumps > 20 s inside a boot) | the system hung (I/O wait, lockup) rather than lost power |
| `Out of memory`, `soft lockup`, `hung_task`, `Oops` in the kernel lines | software / memory - check what ran (`~/.local/state/arstro-remote-<slot>/*.log`) |
| launcher.log shows a restart mid-boot | the server process crashed (Python) - read `crash.log` |

## Findings so far (2026-10-04) - update this section when they change

- 94 boots logged since 2026-09-24, **none clean**. SoC peak at death 28-65 °C; NVMe never above its
  warning temperature; Wi-Fi 41 °C now; no thermal, CT-kill, NVMe, PCIe or I/O error ever logged.
- Many deaths 40-160 s after boot with the big cores at 2304 MHz and load spiking (Cosmo builds,
  recorder) - the brown-out pattern. The long runs (1-5 h) ended idle, the M.2-rail pattern.
- **NVMe SMART: 9,718 power cycles, 6,956 unsafe shutdowns in 122 power-on hours** - two orders of
  magnitude more than the 94 board boots in the same weeks (unless the drive came with that history:
  `nvme-counters.log` will tell - baseline taken 2026-10-04 00:30: cycles=9718, unsafe=6956).
- **USB-C: no PD contract** (`online=0`, mode `default`).
- Verdict: **power supply, not overheating** - the user's "Wi-Fi/NVMe broken, Bluetooth alive"
  symptom is the PCIe side losing power. Fix the supply first: a 5 V / 4 A (or more) supply rated for
  the Orange Pi 5 Plus, a short thick USB-C cable, check the PD contract comes up (`online=1`).
  Meanwhile `cpufreq` can cap the big cores to soften current steps.

## Capturing the next incident

0. The logger itself: `sysmon.sh` + `sysmon.service` here are the copies of record - install with
   `cp sysmon.sh ~/sysmon/ && cp sysmon.service ~/.config/systemd/user/ && systemctl --user enable --now sysmon`.
1. **Mirror the logs off the NVMe**: format a USB stick, label it `SYSMON`, mount it at
   `/media/$USER/SYSMON` (or `/mnt/SYSMON`). `sysmon.sh` then writes every line there too - USB is
   not PCIe, so the stick keeps the lines written while the NVMe is gone (`dev[nvme=GONE]`, the
   kernel's `nvme ... controller is down`, `iwlwifi ... Hardware became unavailable`).
2. **Serial console** (best): the debug UART is `ttyS2` at 1500000 baud (`console=ttyS2` on the
   kernel command line) - a USB-UART on another machine records the kernel's last words even
   through a brown-out.
3. Run `diagnose.py --smart` after every incident so `nvme-counters.log` grows a series.
4. Over Bluetooth the server still runs from RAM when the disk is gone - a future server op could
   return `/dev/kmsg` from memory (procfs/devtmpfs need no disk). Not built yet: an
   `arstro.embedded_server.implement` task if the stick and the UART are not enough.

## Rules

- Read-only: change nothing but the sysmon logger and its logs. The sudo password is given by the
  user and passed on stdin (`sudo -S`), never written anywhere.
- Never reboot the board, stop a slot or port 8000's `hdmi-recorder-server` to "test" - the board is
  shared and a reset destroys the evidence you came for.
- Say which evidence supports each conclusion and what would refute it. "Probably heat" without a
  temperature near a limit is not a finding.
