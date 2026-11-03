#!/usr/bin/env bash
# Look at every Arstro screen on this computer (docs/design.md) without touching what is installed:
# a device manager on localhost from this repository, and as its devices this repository's Arstro
# Remote and backup terminal plus a few demo devices (online and offline, a computer, a phone).
#
#   scripts/preview.sh [start] [--lan]   start (or restart) everything and print where to look
#   scripts/preview.sh keys              the access keys and passwords of the preview devices
#   scripts/preview.sh stop              stop everything and delete the preview's data
#
# Ports: 8099 device manager, 8098 Arstro Remote, 8097 backup terminal (PREVIEW_PORT_BASE=N moves
# them to N, N+1, N+2). --lan lets a phone on this network reach the device manager (the Arstro
# Devices app: add http://<this computer's IP>:8099). Everything lives in ~/.cache/arstro-preview
# with its own config, state and runtime folders: the installed slots, the backup terminal on 8181
# and their keys are never used. The preview Arstro Remote is real: its terminal is a shell here.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D="${ARSTRO_PREVIEW_DIR:-$HOME/.cache/arstro-preview}"
BASE_PORT="${PREVIEW_PORT_BASE:-8097}"
P_BACKUP=$BASE_PORT
P_REMOTE=$((BASE_PORT + 1))
P_RELAY=$((BASE_PORT + 2))
RELAY_PATH=/arstro/internal/devices
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ processes
spawn() {                                     # name dir cmd...: a background process of the preview
    local name=$1 dir=$2
    shift 2
    (cd "$dir" && exec nohup "$@" > "$D/logs/$name.log" 2>&1 < /dev/null) &
    echo $! > "$D/pids/$name"
}
stop_all() {
    [ -d "$D/pids" ] || return 0
    local f pid
    for f in "$D"/pids/*; do
        [ -e "$f" ] || continue
        pid=$(cat "$f")
        # only a process of this preview (its command line names the preview folder or a module of ours)
        if [ -r "/proc/$pid/cmdline" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -e arstro -e fake_device; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$f"
    done
    sleep 1
}
wait_http() {                                  # url seconds
    local i
    for ((i = 0; i < $2 * 10; i++)); do
        curl -fsS -o /dev/null --max-time 1 "$1" 2>/dev/null && return 0
        sleep 0.1
    done
    return 1
}
port_free() {
    ! ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .
}
key_of() {                                     # identity file -> its access key
    python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["key"])' "$1"
}

# ------------------------------------------------------------------ start
start() {
    local lan=0
    [ "${1:-}" = "--lan" ] && lan=1
    stop_all
    rm -rf "${D:?}"
    for p in "$P_BACKUP" "$P_REMOTE" "$P_RELAY"; do
        port_free "$p" || die "port $p is in use (PREVIEW_PORT_BASE=N picks other ports)"
    done
    mkdir -p "$D"/{pids,logs,keys,files} && chmod 700 "$D"
    local token; token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
    local relay_url="http://127.0.0.1:$P_RELAY$RELAY_PATH"
    local host=127.0.0.1
    [ "$lan" = 1 ] && host=0.0.0.0

    say "Device manager on port $P_RELAY"
    spawn relay "$ROOT/relay" env RELAY_HOST=$host RELAY_PORT="$P_RELAY" RELAY_DATA="$D/relay" \
        RELAY_JOIN_TOKEN="$token" RELAY_NAME="Preview" python3 -m arstro_relay
    wait_http "$relay_url/api/health" 15 || die "the device manager did not start (see $D/logs/relay.log)"

    say "Arstro Remote (this repository) on port $P_REMOTE"
    local r="$D/remote" cfg="$D/remote/cfg/arstro-remote-p"
    mkdir -p "$cfg" "$r/state" "$r/data" && mkdir -m 700 -p "$r/run"
    python3 - "$cfg/config.json" "$P_REMOTE" "$D/files" "$relay_url" "$token" "$(hostname) (preview)" <<'EOF'
import json, sys
path, port, files, url, token, name = sys.argv[1:]
json.dump({"web_port": int(port), "web_host": "127.0.0.1", "bluetooth_enabled": False,
           "modules": ["monitor", "system", "camera", "connection", "terminal", "screen", "io", "files", "apps", "relay"],
           "files_roots": [files], "log_level": "info",
           "relay": {"url": url, "token": token, "name": name, "enabled": True}}, open(path, "w"), indent=1)
EOF
    python3 -c 'import secrets; print(secrets.token_urlsafe(9))' > "$cfg/web_token" && chmod 600 "$cfg/web_token"
    echo "Files of the preview Arstro Remote (scripts/preview.sh)" > "$D/files/README.txt"
    spawn remote "$ROOT/server" env ARSTRO_SLOT=p XDG_CONFIG_HOME="$r/cfg" XDG_STATE_HOME="$r/state" \
        XDG_DATA_HOME="$r/data" XDG_RUNTIME_DIR="$r/run" PYTHONPATH="$ROOT/server" python3 -m arstro_remote run
    wait_http "http://127.0.0.1:$P_REMOTE/api/ping" 30 || die "Arstro Remote did not start (see $D/logs/remote.log)"

    say "Backup terminal (this repository) on port $P_BACKUP"
    local b="$D/backup" S="$ROOT/server"
    mkdir -p "$b/cfg/arstro-backup" "$b/state" "$b/app" && mkdir -m 700 -p "$b/run"
    # assembled as server/scripts/install_backup.sh does: its own copy of the relay code and xterm
    cp -r "$S/arstro_backup" "$b/app/"
    mkdir -p "$b/app/arstro_backup/vendor"
    touch "$b/app/arstro_backup/vendor/__init__.py"
    cp "$S/arstro_remote/relay/agent.py" "$S/arstro_remote/relay/e2e.py" "$S/arstro_remote/relay/pipe.py" \
       "$S/arstro_remote/web/ws.py" "$b/app/arstro_backup/vendor/"
    sed -i 's/^from \.\.web import ws as wsmod$/from . import ws as wsmod/' "$b/app/arstro_backup/vendor/agent.py"
    for f in xterm.js xterm.css addon-fit.js; do cp "$S/arstro_remote/web/static/vendor/$f" "$b/app/arstro_backup/static/"; done
    echo "{\"port\": $P_BACKUP, \"bind\": \"127.0.0.1\"}" > "$b/cfg/arstro-backup/config.json"
    local benv=(XDG_CONFIG_HOME="$b/cfg" XDG_STATE_HOME="$b/state" XDG_RUNTIME_DIR="$b/run" PYTHONPATH="$b/app")
    (cd "$b/app" && env "${benv[@]}" python3 -m arstro_backup password --set \
        "$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')" > /dev/null)
    (cd "$b/app" && env "${benv[@]}" python3 -m arstro_backup relay --url "$relay_url" --token "$token" \
        --name "$(hostname) backup (preview)" --on > /dev/null)
    spawn backup "$b/app" env "${benv[@]}" python3 -m arstro_backup run
    wait_http "http://127.0.0.1:$P_BACKUP/" 15 || die "the backup terminal did not start (see $D/logs/backup.log)"

    say "Demo devices"
    demo() {                                   # name id hostname platform version slot
        spawn "$2" "$ROOT" python3 devices_app/tool/fake_device.py --relay "$relay_url" --token "$token" \
            --name "$1" --id "$2" --hostname "$3" --platform "$4" --version "$5" --slot "$6" --key-file "$D/keys/$2"
    }
    demo "Orange Pi 5 Plus" orangepi5plus-a-demo01 orangepi5plus "Linux aarch64 (Orange Pi 5 Plus)" 2.6.0 a
    demo "Pixel 8" pixel8-phone-demo02 pixel8 "Android 15" 1.1.0 phone
    demo "Office NUC" office-nuc-a-demo03 office-nuc "Linux x86_64" 2.5.0 a
    local i
    for ((i = 0; i < 100; i++)); do [ -s "$D/keys/office-nuc-a-demo03" ] && break; sleep 0.1; done
    sleep 1
    # the NUC goes away again: an offline device with a "last seen"
    kill "$(cat "$D/pids/office-nuc-a-demo03")" 2>/dev/null || true
    rm -f "$D/pids/office-nuc-a-demo03"
    for ((i = 0; i < 100; i++)); do
        [ -s "$r/cfg/arstro-remote-p/relay_identity.json" ] && [ -s "$b/cfg/arstro-backup/relay_identity.json" ] && break
        sleep 0.1
    done
    show "$lan"
}

show() {
    local lan="${1:-0}" ip
    echo
    say "Open these"
    echo "  Device manager   http://localhost:$P_RELAY$RELAY_PATH/"
    echo "  Arstro Remote    http://localhost:$P_REMOTE/            (also through the device manager)"
    echo "  Backup terminal  http://localhost:$P_BACKUP/            (also through the device manager)"
    if [ "$lan" = 1 ]; then
        ip=$(ip -4 route get 192.0.2.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
        echo "  Phone app        add the server http://${ip:-<this computer>}:$P_RELAY in Arstro Devices"
    fi
    echo
    echo "  Keys and passwords: scripts/preview.sh keys      Stop and clean up: scripts/preview.sh stop"
}

keys() {
    [ -d "$D/keys" ] || die "the preview is not running (scripts/preview.sh start)"
    local r="$D/remote/cfg/arstro-remote-p" b="$D/backup/cfg/arstro-backup" f
    printf '%-26s %s\n' "Arstro Remote password" "$(cat "$r/web_token")"
    printf '%-26s %s\n' "Arstro Remote key" "$(key_of "$r/relay_identity.json")"
    printf '%-26s %s\n' "Backup terminal password" "$(cat "$b/password")"
    printf '%-26s %s\n' "Backup terminal key" "$(key_of "$b/relay_identity.json")"
    for f in "$D"/keys/*; do
        printf '%-26s %s\n' "$(basename "$f") key" "$(cat "$f")"
    done
}

case "${1:-start}" in
    start) shift || true; start "${1:-}" ;;
    --lan) start --lan ;;
    keys) keys ;;
    stop) stop_all; rm -rf "${D:?}"; say "Stopped, preview data deleted" ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown command $1 (start [--lan], keys, stop)" ;;
esac
