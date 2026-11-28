#!/bin/bash
# The Arstro backup terminal (ADM-09, ADM-10): a terminal-only server on its own port (8181) with its
# own copy of the code, password, start-at-boot service and device-manager identity. Installing or
# updating Arstro Remote's slots never touches it, so the machine keeps a way in while the main
# server is stopped, broken or being updated. Run through the one installer:
#
#   ./install.sh --backup                 install it (port 8181) and start it, now and at every boot;
#                                         when it is installed already: its code stays, only checked
#   ./install.sh --backup --force         replace its code with this checkout's (a deliberate update)
#   ./install.sh --backup --uninstall     stop and remove it (its config stays; --purge removes it)
#
# Options
#   --port N              its port (default 8181; never a slot's port)
#   --password PW         its password (default: slot A's web password, else asked; Enter = admin)
#   --relay URL           its device manager (default: slot A's, when slot A has one switched on)
#   --relay-token T       that device manager's join token (with --relay)
#   --relay-name N        its name in the device list (default: "<host> (backup terminal)")
#   --no-start            write the files only (no service enabled or started)
#   --force-own-session   with --force: restart it although THIS shell runs inside it (it ends)
#   -y, --yes             never ask
set -euo pipefail
# Part of install.sh (SET-08): it refuses to run on its own - set the machine up with ./install.sh.
[ "${1:-}" = --from-install-sh ] && shift || { echo "$(basename "$0") is part of the installer - run ./install.sh instead (./install.sh --help)" >&2; exit 2; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"        # server/
PORT="" PASSWORD="" RELAY_URL="" RELAY_TOKEN="" RELAY_NAME=""
FORCE=0 START=1 UNINSTALL=0 PURGE=0 YES=0 FORCE_OWN=0

usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --backup) shift ;;
        --port) PORT="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --relay) RELAY_URL="$2"; shift 2 ;;
        --relay-token) RELAY_TOKEN="$2"; shift 2 ;;
        --relay-name) RELAY_NAME="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --no-start) START=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge) PURGE=1; shift ;;
        --force-own-session) FORCE_OWN=1; shift ;;
        -y|--yes) YES=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option $1 (install.sh --backup --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "run this as the user the backup runs as, not root"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
RUN_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
APP_DIR="$DATA_HOME/arstro-backup"
CFG_DIR="$CONFIG_HOME/arstro-backup"
LOG_DIR="$STATE_HOME/arstro-backup"
UNIT="$CONFIG_HOME/systemd/user/arstro-backup.service"
BIN="$HOME/.local/bin/arstro-backup"
PY="$(command -v /usr/bin/python3 || command -v python3 || true)"
[ -n "$PY" ] || die "python3 is needed"
HAVE_SYSTEMD=0
command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1 && HAVE_SYSTEMD=1

running_pid() {  # the backup's pid while it runs
    "$PY" - "$RUN_DIR/arstro-backup.json" <<'EOF' 2>/dev/null || true
import json, os, sys
try:
    pid = json.load(open(sys.argv[1]))["pid"]
    os.kill(pid, 0)
    print(pid)
except Exception:
    pass
EOF
}
inside_backup() {  # does this shell run inside the backup's own shells?
    local pid; pid=$(running_pid)
    [ -n "$pid" ] || return 1
    local p=$$
    while [ "$p" -gt 1 ]; do
        [ "$p" = "$pid" ] && return 0
        p=$(awk '/^PPid:/{print $2}' "/proc/$p/status" 2>/dev/null || echo 1)
    done
    return 1
}
our_unit() {  # systemd runs THIS unit file (not one of another home, e.g. a test's)
    [ "$HAVE_SYSTEMD" = 1 ] && [ -f "$UNIT" ] &&
        [ "$(systemctl --user show -p FragmentPath --value arstro-backup.service 2>/dev/null)" = "$UNIT" ]
}
stop_backup() {
    if our_unit; then systemctl --user stop arstro-backup.service 2>/dev/null || true; fi
    local pid; pid=$(running_pid)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
}

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
    if inside_backup && [ "$FORCE_OWN" = 0 ]; then
        die "this shell runs inside the backup terminal - removing it would end this shell (--force-own-session)"
    fi
    say "Removing the backup terminal"
    OURS=0
    our_unit && OURS=1
    [ "$OURS" = 1 ] && { systemctl --user disable arstro-backup.service 2>/dev/null || true; }
    stop_backup
    rm -f "$UNIT" "$BIN"
    [ "$OURS" = 1 ] && { systemctl --user daemon-reload 2>/dev/null || true; }
    rm -rf "$APP_DIR"
    if [ "$PURGE" = 1 ]; then rm -rf "$CFG_DIR" "$LOG_DIR"; echo "   config and logs removed"; else echo "   kept: $CFG_DIR (identity, password), $LOG_DIR"; fi
    exit 0
fi

# ------------------------------------------------------------------ checks
[ -d "$SRC/arstro_backup" ] && [ -d "$SRC/arstro_remote/relay" ] || die "run this from an Arstro Remote checkout (server/arstro_backup is missing here)"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 3.9 or newer is needed"
[ "$HAVE_SYSTEMD" = 1 ] || [ "$START" = 0 ] || die "the backup runs as a systemd user service (systemctl --user does not work here)"
"$PY" -c 'import cryptography' 2>/dev/null || warn "python3-cryptography is missing: the backup works on the network, but not through the device manager (apt install python3-cryptography)"

OLD_PORT=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", ""))' "$CFG_DIR/config.json" 2>/dev/null || true)
PORT="${PORT:-${OLD_PORT:-8181}}"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "bad --port"
for cfg in "$CONFIG_HOME"/arstro-remote*/config.json; do
    [ -f "$cfg" ] || continue
    p=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("web_port", ""))' "$cfg" 2>/dev/null || true)
    [ "$p" = "$PORT" ] && die "port $PORT is $(basename "$(dirname "$cfg")")'s - give the backup another (--port)"
done
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/ping" 2>/dev/null | grep -q '"arstro-backup"' ||
        die "something else listens on port $PORT - give the backup another (--port)"
fi

INSTALLED=0
[ -f "$APP_DIR/arstro_backup/__init__.py" ] && INSTALLED=1
if [ "$INSTALLED" = 1 ] && [ "$FORCE" = 1 ] && inside_backup && [ "$FORCE_OWN" = 0 ]; then
    die "this shell runs inside the backup terminal - replacing its code restarts it and ends this shell (--force-own-session)"
fi

# ------------------------------------------------------------------ the code (frozen copy)
mkdir -p "$DATA_HOME" "$CFG_DIR" "$LOG_DIR" "$(dirname "$UNIT")" "$(dirname "$BIN")"
chmod 700 "$CFG_DIR"
NEW_CODE=0
if [ "$INSTALLED" = 1 ] && [ "$FORCE" = 0 ]; then
    say "The backup terminal is installed (version $(cat "$APP_DIR/arstro_backup/INSTALLED" 2>/dev/null | head -1 || echo '?')) - its code stays as it is (--force replaces it)"
else
    say "Installing the backup terminal's code into $APP_DIR"
    TMP=$(mktemp -d "$DATA_HOME/.arstro-backup.XXXXXX")
    trap 'rm -rf "$TMP"' EXIT
    cp -r "$SRC/arstro_backup" "$TMP/arstro_backup"
    find "$TMP" -name __pycache__ -prune -exec rm -rf {} +
    V="$TMP/arstro_backup/vendor"
    mkdir -p "$V"
    printf '"""Copied from Arstro Remote when the backup was installed - frozen from then on."""\n' > "$V/__init__.py"
    cp "$SRC/arstro_remote/relay/agent.py" "$SRC/arstro_remote/relay/e2e.py" "$SRC/arstro_remote/relay/pipe.py" "$V/"
    cp "$SRC/arstro_remote/web/ws.py" "$V/ws.py"
    sed -i 's/^from \.\.web import ws as wsmod$/from . import ws as wsmod/' "$V/agent.py"
    if grep -n '^from \.\.\|^import arstro_remote\|^from arstro_remote' "$V"/*.py; then
        die "the copied relay code imports more of Arstro Remote than the backup carries (see above)"
    fi
    for f in xterm.js xterm.css addon-fit.js LICENSE.xterm; do
        cp "$SRC/arstro_remote/web/static/vendor/$f" "$TMP/arstro_backup/static/$f"
    done
    COMMIT=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
    VER=$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import arstro_backup; print(arstro_backup.VERSION)' "$TMP")
    printf '%s\nfrom commit %s, installed %s\n' "$VER" "$COMMIT" "$(date -Iseconds)" > "$TMP/arstro_backup/INSTALLED"
    # it must run on its own: nothing of Arstro Remote on its path
    (cd / && env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONPATH="$TMP" "$PY" -c \
        'import arstro_backup.web, arstro_backup.relay, arstro_backup.terms, arstro_backup.main, arstro_backup.vendor.agent') ||
        die "the assembled backup does not import on its own"
    rm -rf "$APP_DIR.old"
    [ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"
    mv "$TMP" "$APP_DIR"
    trap - EXIT
    rm -rf "$APP_DIR.old"
    chmod 755 "$APP_DIR"
    NEW_CODE=1
fi

# ------------------------------------------------------------------ config (kept across reinstalls)
"$PY" - "$CFG_DIR/config.json" "$PORT" <<'EOF'
import json, os, sys
path, port = sys.argv[1], int(sys.argv[2])
try:
    d = json.load(open(path))
except Exception:
    d = {}
d["port"] = port
d.setdefault("bind", "0.0.0.0")
json.dump(d, open(path, "w"), indent=1)
EOF
if [ -n "$PASSWORD" ] || [ ! -s "$CFG_DIR/password" ]; then
    if [ -z "$PASSWORD" ] && [ -s "$CONFIG_HOME/arstro-remote-a/web_token" ]; then
        PASSWORD=$(head -1 "$CONFIG_HOME/arstro-remote-a/web_token")
        echo "   password: the same as slot A's web password"
    elif [ -z "$PASSWORD" ] && [ "$YES" = 0 ] && [ -t 0 ]; then
        read -r -s -p "Password for the backup terminal (Enter = admin): " PASSWORD; echo
    fi
    PASSWORD="${PASSWORD:-admin}"
    (umask 077; printf '%s\n' "$PASSWORD" > "$CFG_DIR/password")
fi
if [ -n "$RELAY_URL$RELAY_TOKEN$RELAY_NAME" ] || [ ! -f "$CFG_DIR/relay.json" ]; then
    "$PY" - "$CFG_DIR/relay.json" "$CONFIG_HOME/arstro-remote-a/relay.json" "$RELAY_URL" "$RELAY_TOKEN" "$RELAY_NAME" <<'EOF'
import json, os, sys
path, slot_a, url, token, name = sys.argv[1:6]
try:
    d = json.load(open(path))
except Exception:
    d = {}
if not (url or token) and not d:
    try:                                             # slot A's device manager, when it uses one
        a = json.load(open(slot_a))
        if a.get("enabled") and a.get("url") and a.get("token"):
            url, token = a["url"], a["token"]
            print("   device manager: the same as slot A's (%s)" % url)
    except Exception:
        pass
if url:
    d["url"] = url
if token:
    d["token"] = token
if name:
    d["name"] = name
d["enabled"] = bool(d.get("url") and d.get("token"))
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(d, f, indent=1)
os.replace(path + ".tmp", path)
EOF
fi

# ------------------------------------------------------------------ command + service
cat > "$BIN" <<EOF
#!/bin/sh
# the Arstro backup terminal's command (install.sh --backup). It starts in its own folder: python -m
# looks in the current folder first, and a checkout's server/ holds another arstro_backup.
cd "$APP_DIR" && PYTHONPATH="$APP_DIR" exec "$PY" -m arstro_backup "\$@"
EOF
chmod 755 "$BIN"
cat > "$UNIT" <<EOF
[Unit]
Description=Arstro backup terminal (port $PORT) - never updated with the Arstro Remote slots
After=network-online.target
StartLimitIntervalSec=0

[Service]
WorkingDirectory=$APP_DIR
Environment=PYTHONPATH=$APP_DIR
ExecStart=$PY -m arstro_backup run
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
[ "$START" = 1 ] || { say "Installed (files only: --no-start)"; exit 0; }
systemctl --user daemon-reload
systemctl --user enable arstro-backup.service >/dev/null 2>&1 || warn "could not enable the service at boot"
if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo no)" != yes ]; then
    warn "it starts only when $(id -un) logs in - to have it from boot: sudo loginctl enable-linger $(id -un)"
fi
if [ "$NEW_CODE" = 1 ] || [ -n "$PASSWORD$RELAY_URL$RELAY_TOKEN$RELAY_NAME" ] || [ "$OLD_PORT" != "$PORT" ]; then
    systemctl --user restart arstro-backup.service
else
    systemctl --user start arstro-backup.service
fi
for _ in $(seq 1 30); do
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1 && break
    sleep 0.5
done
curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1 || die "the backup terminal did not answer on port $PORT - see $LOG_DIR/backup.log and: journalctl --user -u arstro-backup"
say "The backup terminal runs on port $PORT"
for a in $(hostname -I 2>/dev/null); do [[ "$a" == *:* ]] || echo "   http://$a:$PORT/"; done
sleep 2
"$BIN" status | sed -n '/^Device/p' | sed 's/^/   /'
echo "   its access key (to open it through the device manager): arstro-backup key"
