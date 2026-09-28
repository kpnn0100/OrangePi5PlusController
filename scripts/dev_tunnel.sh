#!/usr/bin/env bash
# Developer tunnel: lets the Android app reach the Pi daemon without Bluetooth.
#
#   tools/dev_tunnel.sh up   user@PI [ANDROID_SERIAL]   then connect to  tcp:127.0.0.1:7788  in the app
#   tools/dev_tunnel.sh down user@PI [ANDROID_SERIAL]
#   tools/dev_tunnel.sh status user@PI
#
# Path: app -> device 127.0.0.1:7788 -(adb reverse)-> this PC 127.0.0.1:7788
#       -(ssh -L)-> the daemon's control socket /run/user/<uid>/arstro-remote.sock on the Pi.
# Uses an SSH control socket, so "down" never has to pkill by pattern.
# The same PC port also feeds: ARSTRO_TCP=127.0.0.1:7788 flutter test test/reconnect_live_test.dart
set -euo pipefail
cmd=${1:-}; pi=${2:-}; serial=${3:-${ANDROID_SERIAL:-}}
[ -n "$cmd" ] && [ -n "$pi" ] || { sed -n '2,12p' "$0"; exit 2; }
PORT=${ARSTRO_TUNNEL_PORT:-7788}
CTL="${XDG_RUNTIME_DIR:-/tmp}/arstro-tunnel-%C"   # short path: unix sockets max ~100 chars
ADB=(adb); [ -n "$serial" ] && ADB+=(-s "$serial")

case "$cmd" in
  up)
    uid=$(ssh -o BatchMode=yes "$pi" id -u)
    ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -M -S "$CTL" -f -N \
        -L "127.0.0.1:$PORT:/run/user/$uid/arstro-remote.sock" "$pi"
    if command -v adb >/dev/null && "${ADB[@]}" get-state >/dev/null 2>&1; then
        "${ADB[@]}" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null && echo "adb reverse tcp:$PORT set"
    fi
    echo "tunnel up: 127.0.0.1:$PORT -> $pi:/run/user/$uid/arstro-remote.sock"
    echo "in the app: Connect by Bluetooth address…  ->  tcp:127.0.0.1:$PORT" ;;
  down)
    ssh -S "$CTL" -O exit "$pi" 2>/dev/null || true
    command -v adb >/dev/null && "${ADB[@]}" reverse --remove "tcp:$PORT" 2>/dev/null || true
    echo "tunnel down" ;;
  status)
    ssh -S "$CTL" -O check "$pi" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
