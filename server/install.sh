#!/bin/bash
# Arstro Remote server installer - one script for any Linux machine (Debian/Ubuntu family,
# Raspberry Pi OS, Orange Pi / Armbian images, x86 PCs). Run it as the user the server
# should run as (normally the desktop auto-login user), NOT as root; it asks for sudo only
# for packages and device permissions.
#
#   ./install.sh                         install / update slot A on port 8080
#   ./install.sh --slot b                install / update slot B on port 8081 (A keeps running)
#
# Slots (A/B, ADM-06): two instances side by side, each with its own code, config, logs,
# socket, port and start-at-boot entry, so a new version goes into the slot you are NOT
# using, gets tested there, and only then do you switch (see --activate).
#
# Options
#   --slot a|b|...        instance to install (default a)
#   --port N              web port (default 8080 for a, 8081 for b, ...)
#   --password PW         web / remote-CLI password; without it a new slot asks for one
#                         (Enter = "admin"); an existing slot keeps its password
#   --modules LIST        comma list of modules or "all" (default all): monitor, system,
#                         camera, connection, terminal, screen, io, files
#   --bluetooth on|off    own the app's Bluetooth link (default: on for a, off for others -
#                         only one instance per machine can own it)
#   --boot desktop|systemd|none
#                         start at boot through the desktop session's autostart (needed for
#                         Wi-Fi control without extra polkit rules and for the remote screen),
#                         a systemd user service (headless machines), or not at all.
#                         Default: desktop when a graphical session is installed, else systemd
#   --activate            make this slot the default for the `arstro-remote` command and
#                         give it the Bluetooth link (the other slots lose it)
#   --no-deps             do not install packages
#   --no-perms            do not set up device permissions (udev rules, groups)
#   --no-start            install only
#   --uninstall           stop and remove this slot (config and logs stay; --purge removes them)
#   --force-own-session   install into the slot that hosts THIS shell anyway (it gets killed)
#   -y, --yes             never ask (password defaults to "admin" for a new slot)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE="$SRC"                                   # the installed copy lives in <slot>/bin/
[ -d "$CODE/arstro_remote" ] || [ ! -d "$SRC/../arstro_remote" ] || CODE="$(cd "$SRC/.." && pwd)"
SLOT=a PORT="" PASSWORD="" MODULES="all" BT="" BOOT="" ACTIVATE=0
DEPS=1 PERMS=1 START=1 UNINSTALL=0 PURGE=0 YES=0 FORCE_OWN=0

usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --slot) SLOT="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --modules) MODULES="$2"; shift 2 ;;
        --bluetooth) BT="$2"; shift 2 ;;
        --boot) BOOT="$2"; shift 2 ;;
        --activate) ACTIVATE=1; shift ;;
        --no-deps) DEPS=0; shift ;;
        --no-perms) PERMS=0; shift ;;
        --no-start) START=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge) PURGE=1; shift ;;
        -y|--yes) YES=1; shift ;;
        --force-own-session) FORCE_OWN=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option $1" >&2; usage 2 ;;
    esac
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "run this as the user the server runs as (the desktop user), not root"
[[ "$SLOT" =~ ^[a-z]$ ]] || die "--slot must be one letter (a, b, ...)"
SLOT_UP=$(echo "$SLOT" | tr a-z A-Z)
IDX=$(( $(printf '%d' "'$SLOT") - 97 ))
PORT="${PORT:-$((8080 + IDX))}"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "bad --port"
[ -n "$BT" ] || { [ "$SLOT" = a ] && BT=on || BT=off; }
[[ "$BT" =~ ^(on|off)$ ]] || die "--bluetooth on|off"

NAME="arstro-remote-$SLOT"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
RUN_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
APP_DIR="$DATA_HOME/$NAME"
CFG_DIR="$CONFIG_HOME/$NAME"
LOG_DIR="$STATE_HOME/$NAME"
BIN_DIR="$HOME/.local/bin"
AUTOSTART="$CONFIG_HOME/autostart/$NAME.desktop"
UNIT="$CONFIG_HOME/systemd/user/$NAME.service"
LEGACY_CFG="$CONFIG_HOME/arstro-remote"
ACTIVE_FILE="$CONFIG_HOME/arstro-remote-active"

ALL_MODULES="monitor system camera connection terminal screen io files"
if [ "$MODULES" = all ]; then MOD_LIST="$ALL_MODULES"; MOD_JSON=null
else
    MOD_LIST=$(echo "$MODULES" | tr ',' ' ')
    for m in $MOD_LIST; do [[ " $ALL_MODULES " == *" $m "* ]] || die "unknown module $m (have: $ALL_MODULES)"; done
    MOD_JSON="[$(for m in $MOD_LIST; do printf '"%s",' "$m"; done | sed 's/,$//')]"
fi
has_mod() { [[ " $MOD_LIST " == *" $1 "* ]]; }

launcher_pid() {  # pid of this slot's running launcher, or nothing
    local f="$RUN_DIR/$NAME-launcher.lock" pid
    [ -f "$f" ] || return 0
    if flock -n "$f" true 2>/dev/null; then return 0; fi     # lock free: not running
    pid=$(head -c 20 "$f" | tr -dc 0-9)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && echo "$pid"
    return 0
}
daemon_pid() {
    local f="$RUN_DIR/$NAME.lock" pid
    [ -f "$f" ] || return 0
    if flock -n "$f" true 2>/dev/null; then return 0; fi
    pid=$(head -c 20 "$f" | tr -dc 0-9)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && echo "$pid"
    return 0
}
stop_slot() {
    if [ -f "$UNIT" ] && command -v systemctl >/dev/null; then systemctl --user stop "$NAME.service" 2>/dev/null || true; fi
    local lp; lp=$(launcher_pid)
    [ -n "$lp" ] && kill "$lp" 2>/dev/null || true
    local dp; dp=$(daemon_pid)
    [ -n "$dp" ] && kill "$dp" 2>/dev/null || true
    for _ in $(seq 1 30); do [ -z "$(daemon_pid)" ] && break; sleep 0.5; done
}

# ------------------------------------------------------------------ rule zero (ADM-08)
# A shell in a slot's web terminal is that slot's child: reinstalling or restarting that slot
# kills the shell running this script (and whatever started it). Refuse, and name the idle slot.
HOST_SLOT=$(PYTHONPATH="$CODE" python3 -c "import os; from arstro_remote import slots; print(slots.current($$) or '-')" 2>/dev/null || echo -)
# (--no-start only swaps files; the running daemon keeps its code until it restarts, so that is safe)
if { [ "$HOST_SLOT" = "$SLOT" ] || { [ "$HOST_SLOT" = legacy ] && [ "$SLOT" = a ]; }; } &&
   { [ "$START" = 1 ] || [ "$UNINSTALL" = 1 ]; }; then
    IDLE=$(PYTHONPATH="$CODE" python3 -c "from arstro_remote import slots; print(slots.idle($$) or '-')" 2>/dev/null)
    if [ "$FORCE_OWN" = 1 ]; then
        warn "this shell runs inside slot $HOST_SLOT - it will be killed by this install (--force-own-session)"
    else
        die "this shell runs inside slot $HOST_SLOT: installing slot $SLOT_UP would kill it. Deploy into the idle slot (--slot ${IDLE:-?}), or pass --force-own-session"
    fi
fi

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
    say "Removing slot $SLOT_UP"
    stop_slot
    [ -f "$UNIT" ] && { systemctl --user disable "$NAME.service" 2>/dev/null || true; rm -f "$UNIT"; }
    rm -f "$AUTOSTART" "$BIN_DIR/$NAME"
    rm -rf "$APP_DIR"
    [ "$PURGE" = 1 ] && rm -rf "$CFG_DIR" "$LOG_DIR"
    if [ -L "$BIN_DIR/arstro-remote" ] && [ "$(readlink "$BIN_DIR/arstro-remote")" = "$NAME" ]; then rm -f "$BIN_DIR/arstro-remote"; fi
    echo "Slot $SLOT_UP removed$([ "$PURGE" = 1 ] || echo " (config and logs kept in $CFG_DIR, $LOG_DIR)")."
    exit 0
fi

command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 >= 3.9 is required"

# ------------------------------------------------------------------ boot mode
if [ -z "$BOOT" ]; then
    if ls /usr/share/xsessions/*.desktop >/dev/null 2>&1 || [ -n "${DISPLAY:-}" ]; then BOOT=desktop; else BOOT=systemd; fi
fi
[[ "$BOOT" =~ ^(desktop|systemd|none)$ ]] || die "--boot desktop|systemd|none"

SUDO_OK=""
need_sudo() {   # ask once
    [ -n "$SUDO_OK" ] && return 0
    if sudo -n true 2>/dev/null; then SUDO_OK=1; return 0; fi
    if [ "$YES" = 1 ] && ! [ -t 0 ]; then return 1; fi
    echo "   (sudo is needed for: $1)"
    sudo -v && SUDO_OK=1
}

# ------------------------------------------------------------------ 1. packages
if [ "$DEPS" = 1 ]; then
    say "Checking packages for: $MOD_LIST"
    missing=$(bash "$SRC/scripts/install_deps.sh" --check $MOD_LIST)
    if [ -n "$missing" ]; then
        echo "   missing: $missing"
        if need_sudo "installing packages"; then
            sudo bash "$SRC/scripts/install_deps.sh" $MOD_LIST || die "required packages could not be installed"
        else
            warn "cannot use sudo - install them yourself: sudo apt install $missing"
        fi
    fi
fi
python3 -c 'import dbus, psutil; from gi.repository import GLib' 2>/dev/null ||
    die "python3-dbus, python3-psutil and python3-gi are required (run without --no-deps)"

# ------------------------------------------------------------------ 2. device permissions
if [ "$PERMS" = 1 ] && has_mod io; then
    say "IO device permissions (GPIO, I2C, SPI, serial, PWM, LEDs)"
    want=$(grep -o 'arstro-io-rules v[0-9]*' "$SRC/scripts/setup_io_perms.sh" | head -1)
    if ! grep -qs "$want" /etc/udev/rules.d/70-arstro-io.rules || ! getent group gpio | grep -qw "$(id -un)"; then
        if need_sudo "udev rules and groups for the IO devices"; then
            sudo bash "$SRC/scripts/setup_io_perms.sh" "$(id -un)" $([ "$BOOT" = systemd ] && echo --headless) ||
                warn "device permissions could not be set up"
        else
            warn "no sudo - IO Control stays read-only (run: sudo $SRC/scripts/setup_io_perms.sh $(id -un))"
        fi
    else
        echo "   already set up"
    fi
elif [ "$PERMS" = 1 ] && [ "$BOOT" = systemd ]; then
    need_sudo "polkit rule for NetworkManager" && sudo bash "$SRC/scripts/setup_io_perms.sh" "$(id -un)" --headless || true
fi
for g in bluetooth netdev; do
    getent group "$g" >/dev/null && ! id -nG | tr ' ' '\n' | grep -qx "$g" && warn "$(id -un) is not in group '$g' (sudo usermod -aG $g $(id -un))"
done

# ------------------------------------------------------------------ 3. code
say "Installing slot $SLOT_UP into $APP_DIR"
mkdir -p "$DATA_HOME" "$BIN_DIR" "$CFG_DIR" "$LOG_DIR"
rm -rf "$APP_DIR.new"
mkdir -p "$APP_DIR.new/bin"
cp -r "$CODE/arstro_remote" "$APP_DIR.new/"
find "$APP_DIR.new" -name '__pycache__' -prune -exec rm -rf {} +
install -m 755 "$SRC/scripts/arstro-remote-launcher" "$APP_DIR.new/bin/arstro-remote-launcher"
install -m 755 "$SRC/install.sh" "$APP_DIR.new/bin/install.sh"
mkdir -p "$APP_DIR.new/bin/scripts"
install -m 755 "$SRC/scripts/install_deps.sh" "$SRC/scripts/setup_io_perms.sh" "$SRC/scripts/arstro-remote-launcher" "$APP_DIR.new/bin/scripts/"
python3 - "$APP_DIR.new" <<'EOF'
import compileall, sys
compileall.compile_dir(sys.argv[1] + "/arstro_remote", quiet=1)
EOF
rm -rf "$APP_DIR.old"
[ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"
mv "$APP_DIR.new" "$APP_DIR"
rm -rf "$APP_DIR.old"
cat >"$BIN_DIR/$NAME" <<EOF
#!/bin/sh
# Arstro Remote CLI for slot $SLOT_UP (install.sh)
export ARSTRO_SLOT=$SLOT
export PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m arstro_remote "\$@"
EOF
chmod 755 "$BIN_DIR/$NAME"
# `arstro-remote` = the active slot; a first install makes itself active
if [ ! -e "$BIN_DIR/arstro-remote" ] && [ ! -e "$ACTIVE_FILE" ]; then
    ACTIVATE=1
fi

# ------------------------------------------------------------------ 4. config
NEW_SLOT=0
[ -f "$CFG_DIR/config.json" ] || NEW_SLOT=1
if [ "$NEW_SLOT" = 1 ]; then
    # seed a new slot from another installed one (or an old unslotted install): same
    # recorder / screen settings and the same web password
    for d in "$CONFIG_HOME"/arstro-remote-[a-z] "$LEGACY_CFG"; do
        [ -d "$d" ] && [ "$d" != "$CFG_DIR" ] || continue
        for f in recorder.json screen.json web_token web_open; do
            [ -f "$d/$f" ] && [ ! -f "$CFG_DIR/$f" ] && cp -p "$d/$f" "$CFG_DIR/$f"
        done
        echo "   settings and password copied from $d"
        break
    done
fi
python3 - "$CFG_DIR/config.json" "$PORT" "$BT" "$MOD_JSON" "$((22 + IDX))" "$NEW_SLOT" <<'EOF'
import json, os, sys
path, port, bt, mods, channel, new = sys.argv[1:7]
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {"alias": "Arstro-{hostname}", "pairing": "window", "pair_window_sec": 600}
cfg["web_port"] = int(port)
cfg["bluetooth_enabled"] = bt == "on"
cfg["modules"] = json.loads(mods)
if new == "1" or "channel" not in cfg:
    cfg["channel"] = int(channel)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
EOF
echo "   $CFG_DIR/config.json: port $PORT, bluetooth $BT, modules ${MODULES}"

# password (SEC-03): asked for a new slot, default "admin"
TOKEN="$CFG_DIR/web_token"
if [ -z "$PASSWORD" ] && [ ! -s "$TOKEN" ]; then
    if [ "$YES" = 0 ] && [ -t 0 ]; then
        read -r -s -p "   Web / remote-CLI password for slot $SLOT_UP (Enter = admin): " PASSWORD; echo
        if [ -n "$PASSWORD" ]; then
            read -r -s -p "   Again: " again; echo
            [ "$again" = "$PASSWORD" ] || die "the passwords differ"
        fi
    fi
    PASSWORD="${PASSWORD:-admin}"
    [ "$PASSWORD" = admin ] && warn "the web password is 'admin' - change it (System › Web access, or: $NAME web --set-password)"
fi
if [ -n "$PASSWORD" ]; then
    [ ${#PASSWORD} -ge 5 ] || die "the password needs at least 5 characters"
    (umask 077; printf '%s\n' "$PASSWORD" >"$TOKEN.tmp" && mv -f "$TOKEN.tmp" "$TOKEN")
    echo "   web password set"
fi

# --activate: this slot gets the default command and the Bluetooth link
if [ "$ACTIVATE" = 1 ]; then
    echo "$SLOT" >"$ACTIVE_FILE"
    ln -sfn "$NAME" "$BIN_DIR/arstro-remote"
    for d in "$CONFIG_HOME"/arstro-remote-[a-z]; do
        [ "$d" != "$CFG_DIR" ] && [ -f "$d/config.json" ] || continue
        python3 - "$d/config.json" <<'EOF'
import json, sys
p = sys.argv[1]
c = json.load(open(p)); c["bluetooth_enabled"] = False
json.dump(c, open(p, "w"), indent=2)
EOF
        echo "   Bluetooth link removed from ${d##*-} (restart that slot to apply)"
    done
    python3 - "$CFG_DIR/config.json" <<'EOF'
import json, sys
p = sys.argv[1]
c = json.load(open(p)); c["bluetooth_enabled"] = True
json.dump(c, open(p, "w"), indent=2)
EOF
fi

# ------------------------------------------------------------------ 5. start at boot
LAUNCH="$APP_DIR/bin/arstro-remote-launcher"
case "$BOOT" in
    desktop)
        mkdir -p "$(dirname "$AUTOSTART")"
        cat >"$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Arstro Remote $SLOT_UP
Comment=Arstro Remote server, slot $SLOT_UP (port $PORT)
Exec=env ARSTRO_SLOT=$SLOT $LAUNCH
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
EOF
        [ -f "$UNIT" ] && { systemctl --user disable --now "$NAME.service" 2>/dev/null || true; rm -f "$UNIT"; }
        echo "   starts with the desktop session: $AUTOSTART" ;;
    systemd)
        mkdir -p "$(dirname "$UNIT")"
        cat >"$UNIT" <<EOF
[Unit]
Description=Arstro Remote server, slot $SLOT_UP (port $PORT)
After=network-online.target

[Service]
Environment=ARSTRO_SLOT=$SLOT
ExecStart=$LAUNCH
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
        rm -f "$AUTOSTART"
        systemctl --user daemon-reload
        systemctl --user enable "$NAME.service" >/dev/null
        echo "   systemd user service: $UNIT" ;;
    none)
        rm -f "$AUTOSTART"
        [ -f "$UNIT" ] && { systemctl --user disable "$NAME.service" 2>/dev/null || true; rm -f "$UNIT"; }
        echo "   not started at boot (--boot none)" ;;
esac

# an old, unslotted install (arstro-remote.desktop) that runs on the same port is replaced
LEGACY_AUTOSTART="$CONFIG_HOME/autostart/arstro-remote.desktop"
if [ -f "$LEGACY_AUTOSTART" ] && python3 -c "import json,sys; sys.exit(json.load(open('$LEGACY_CFG/config.json')).get('web_port', 8080) != $PORT)" 2>/dev/null; then
    say "Replacing the old unslotted install (same port $PORT)"
    mv -f "$LEGACY_AUTOSTART" "$LEGACY_AUTOSTART.replaced-by-$NAME"
    pkill -u "$(id -u)" -f '^/bin/bash .*/\.local/bin/arstro-remote-launcher' 2>/dev/null || true
    legacy_pid=$(head -c 20 "$RUN_DIR/arstro-remote.lock" 2>/dev/null | tr -dc 0-9)
    [ -n "$legacy_pid" ] && kill "$legacy_pid" 2>/dev/null || true
    sleep 2
fi

# ------------------------------------------------------------------ 6. (re)start
if [ "$START" = 1 ]; then
    if [ "$BOOT" = systemd ]; then
        say "Restarting $NAME.service"
        systemctl --user restart "$NAME.service"
    elif [ -n "$(launcher_pid)" ]; then
        say "Restarting the slot $SLOT_UP daemon (its launcher starts the new code)"
        dp=$(daemon_pid)
        if [ -n "$dp" ]; then
            kill "$dp" 2>/dev/null || true
            for _ in $(seq 1 60); do kill -0 "$dp" 2>/dev/null || break; sleep 0.5; done   # graceful stop
        fi
    elif [ "$BOOT" = desktop ] && { [ -n "${DISPLAY:-}" ] || [ -S /tmp/.X11-unix/X0 ]; } && [ -z "${SSH_CONNECTION:-}" ]; then
        say "Starting slot $SLOT_UP in this desktop session"
        ARSTRO_SLOT=$SLOT setsid "$LAUNCH" >/dev/null 2>&1 </dev/null &
    elif [ "$BOOT" = desktop ]; then
        say "Installed. Slot $SLOT_UP starts with the desktop session (reboot, or log out and in)."
        echo "   (started from SSH it would lack the session's rights for Wi-Fi control)"
        START=0
    else
        START=0
    fi
fi

if [ "$START" = 1 ]; then
    for _ in $(seq 1 120); do
        curl -fsS "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1 && break
        sleep 0.5
    done
    if curl -fsS "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; then
        say "Slot $SLOT_UP is running"
        for a in $(hostname -I 2>/dev/null); do [[ "$a" == *:* ]] || echo "   http://$a:$PORT/"; done
    else
        warn "slot $SLOT_UP did not answer on port $PORT yet - see $LOG_DIR/launcher.log and arstro-remote.log"
    fi
fi
echo "Done. CLI: $NAME status · logs: $NAME log -f · remove: $APP_DIR/bin/install.sh --slot $SLOT --uninstall"
