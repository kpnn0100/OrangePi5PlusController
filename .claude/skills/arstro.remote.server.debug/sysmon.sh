#!/bin/bash
# Logs system health every $INTERVAL seconds, fsync'd so data survives a hard reset.
# 2026-10-04: + every hwmon by NAME (incl. iwlwifi = the AX200 Wi-Fi), the USB-C power contract,
# whether the NVMe controller / Wi-Fi / Ethernet are still there - and a MIRROR of both logs on a
# USB stick labelled SYSMON when one is mounted: the NVMe holds these logs, so when it drops off the
# PCIe bus (with the Wi-Fi, same bus) nothing on it records the moment; USB is not PCIe.
INTERVAL=${INTERVAL:-5}
DIR=$HOME/sysmon
LOG=$DIR/health.log
KLOG=$DIR/kernel.log
BOOT=$(cat /proc/sys/kernel/random/boot_id)

mirror() { ls -d /media/$USER/SYSMON /media/SYSMON /mnt/SYSMON 2>/dev/null | head -1; }

out() { # $1 = file name (health.log | kernel.log), rest = line
  local f=$1; shift
  echo "$*" >> "$DIR/$f"; sync -d "$DIR/$f" 2>/dev/null
  local m; m=$(mirror)
  if [ -n "$m" ]; then echo "$*" >> "$m/$f"; sync -d "$m/$f" 2>/dev/null; fi
}

rotate() { # keep logs under ~20MB
  for f in "$@"; do
    [ -f "$f" ] && [ "$(stat -c%s "$f")" -gt 20000000 ] && mv -f "$f" "$f.1"
  done
}

out health.log "=== BOOT $BOOT at $(date '+%F %T') ==="

( journalctl -k -f -n 0 -o short-iso 2>/dev/null | while IFS= read -r l; do out kernel.log "$l"; done ) &

while true; do
  t=""
  for h in /sys/class/hwmon/hwmon*; do
    n=$(cat $h/name 2>/dev/null); [ -r $h/temp1_input ] || continue
    t+="${n%_thermal}=$(( $(cat $h/temp1_input)/1000 )) "
  done
  pd="$(cat /sys/class/typec/port0/power_operation_mode 2>/dev/null)/online=$(cat /sys/class/power_supply/tcpm-source-psy-*/online 2>/dev/null | head -1)"
  dev="nvme=$(cat /sys/class/nvme/nvme0/state 2>/dev/null || echo GONE)"
  for i in /sys/class/net/wl* /sys/class/net/en*; do [ -e $i ] && dev+=" ${i##*/}=$(cat $i/operstate)"; done
  f=$(awk '{printf "%d ", $1/1000}' /sys/devices/system/cpu/cpu{0,4,6}/cpufreq/scaling_cur_freq)
  load=$(cut -d' ' -f1-3 /proc/loadavg)
  mem=$(awk '/MemAvailable/{a=$2}/SwapFree/{s=$2}END{printf "avail=%dM swapfree=%dM", a/1024, s/1024}' /proc/meminfo)
  out health.log "$(date '+%F %T') up=$(cut -d. -f1 /proc/uptime)s load=[$load] $mem temp[$t] mhz[$f] pd=$pd dev[$dev]"
  rotate "$LOG" "$KLOG"
  sleep "$INTERVAL"
done
