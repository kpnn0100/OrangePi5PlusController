#!/usr/bin/env bash
# Arstro Remote - set up (or update) an Orange Pi over SSH in one go.
#
#   ./setup_pi.sh [options] [user@]HOST
#
# You only give the address and the password. The script:
#   1. logs in with the password once and installs this machine's SSH key
#      (so later runs, and agents, need no password)
#   2. checks the Pi: Bluetooth adapter, packages, groups, desktop auto-login
#   3. installs missing packages / fixes groups with sudo (same password)
#   4. copies server/ to ~/arstro-remote-src and runs install.sh (restarts the service)
#   5. verifies the service (and can reboot, set the web password, run the tests,
#      install the APK)
#
# Options
#   -p, --password PASS  password (better: export ARSTRO_PASS=..., or type it when asked)
#   --port PORT          SSH port (default 22)
#   --autologin          turn on LightDM auto-login for the user (the service starts with
#                        the desktop session, so without auto-login it waits for a login)
#   --reboot             reboot the Pi at the end if the service is not running yet,
#                        then wait until it is up
#   --web-password       set the web / remote-CLI password (asked, or ARSTRO_WEB_PASS=...)
#   --slot a|b           the server slot to install (default a = port 8080; b = 8081 runs
#                        next to a, for trying a new version - ADM-06)
#   --test               run the on-Pi test suites afterwards (the input test opens a small
#                        window on the Pi screen for ~5 s; the recorder tests use a test
#                        pattern, not the HDMI input)
#   --apk                also install release/*.apk on the Android device attached via adb
#   --apk-serial SERIAL  same, on a specific adb device
#   --no-deps            do not install packages / change groups (no sudo used)
#   --no-key             do not install the SSH key (password is used for every step)
#   -h, --help
#
# Works from Linux, macOS or WSL. Password entry uses sshpass if installed, otherwise
# OpenSSH's SSH_ASKPASS (OpenSSH >= 8.4), otherwise ssh asks you directly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="" PASS="${ARSTRO_PASS:-}" PORT=22 AUTOLOGIN=0 REBOOT=0 RUN_TESTS=0 NO_DEPS=0 NO_KEY=0 SLOT="${ARSTRO_SLOT:-a}"
APK=0 APK_SERIAL="" WEB_PASS_SET=0 WEB_PASS="${ARSTRO_WEB_PASS:-}"
[ -n "$WEB_PASS" ] && WEB_PASS_SET=1

usage() { sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -p|--password) PASS="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --autologin) AUTOLOGIN=1; shift ;;
        --reboot) REBOOT=1; shift ;;
        --test) RUN_TESTS=1; shift ;;
        --web-password) WEB_PASS_SET=1; shift ;;
        --apk) APK=1; shift ;;
        --apk-serial) APK=1; APK_SERIAL="$2"; shift 2 ;;
        --no-deps) NO_DEPS=1; shift ;;
        --no-key) NO_KEY=1; shift ;;
        --slot) SLOT="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option $1" >&2; usage 2 ;;
        *) TARGET="$1"; shift ;;
    esac
done
[ -n "$TARGET" ] || { echo "error: give the Pi as [user@]HOST" >&2; usage 2; }
[[ "$SLOT" =~ ^[a-z]$ ]] || { echo "error: --slot must be one letter (a, b)" >&2; exit 2; }
WEB_PORT=$(( 8080 + $(printf '%d' "'$SLOT") - 97 ))
CLI="~/.local/bin/arstro-remote-$SLOT"
c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

SSH_BASE=(-p "$PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15)
# extra ssh options, e.g. ARSTRO_SSH_OPTS="-o UserKnownHostsFile=/tmp/kh"
# shellcheck disable=SC2206
[ -n "${ARSTRO_SSH_OPTS:-}" ] && SSH_BASE+=($ARSTRO_SSH_OPTS)
KEY_OPTS=()
ASKPASS=""
STATUS_FILE=$(mktemp)
cleanup() { rm -f "$STATUS_FILE"; [ -z "$ASKPASS" ] || rm -f "$ASKPASS"; }
trap cleanup EXIT

ask() {  # ask VAR "prompt" [silent] - reads from the terminal, never blocks without one
    local __v
    if { exec 3</dev/tty; } 2>/dev/null; then
        if [ "${3:-}" = silent ]; then read -rsp "$2" __v <&3; echo >&2; else read -rp "$2" __v <&3; fi
        exec 3<&-
    else
        return 1
    fi
    printf -v "$1" '%s' "$__v"
}

need_password() {
    [ -n "$PASS" ] && return 0
    ask PASS "Password for $TARGET: " silent && return 0
    c_err "a password is needed but there is no terminal to ask - set ARSTRO_PASS=..."
    return 1
}

if [[ "$TARGET" != *@* ]]; then
    ask u "User name on $TARGET [orangepi]: " || u=""
    TARGET="${u:-orangepi}@$TARGET"
fi
PI_USER="${TARGET%@*}"

ssh_version_ok() {  # OpenSSH >= 8.4 has SSH_ASKPASS_REQUIRE=force
    local v; v=$(ssh -V 2>&1 | sed -n 's/^OpenSSH_\([0-9]*\)\.\([0-9]*\).*/\1 \2/p')
    [ -n "$v" ] && set -- $v && { [ "$1" -gt 8 ] || { [ "$1" -eq 8 ] && [ "$2" -ge 4 ]; }; }
}

ssh_pw() {  # ssh using the password
    need_password || return 1
    local opts=("${SSH_BASE[@]}" -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive)
    if command -v sshpass >/dev/null && [ -z "${ARSTRO_NO_SSHPASS:-}" ]; then
        SSHPASS="$PASS" sshpass -e ssh "${opts[@]}" "$TARGET" "$@"
    elif ssh_version_ok; then
        if [ -z "$ASKPASS" ]; then
            ASKPASS=$(mktemp); printf '#!/bin/sh\nprintf "%%s\\n" "$ARSTRO_ASKPASS_PW"\n' >"$ASKPASS"; chmod 700 "$ASKPASS"
        fi
        ARSTRO_ASKPASS_PW="$PASS" SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-:0}" \
            ssh "${opts[@]}" "$TARGET" "$@"
    else
        ssh "${opts[@]}" "$TARGET" "$@"   # ssh prompts on the terminal
    fi
}

KEY_OK=0
key_works() { ssh -o BatchMode=yes "${SSH_BASE[@]}" "${KEY_OPTS[@]}" "$TARGET" true 2>/dev/null; }
remote() {
    if [ "$KEY_OK" = 1 ]; then ssh -o BatchMode=yes "${SSH_BASE[@]}" "${KEY_OPTS[@]}" "$TARGET" "$@"
    else ssh_pw "$@"; fi
}

rsudo() {  # run a command as root on the Pi (password fed to sudo -S, never on a command line)
    if remote 'sudo -n true' 2>/dev/null; then remote "sudo $*"; return; fi
    need_password || return 1
    printf '%s\n' "$PASS" | remote "sudo -S -k -p '' $*"
}

# ------------------------------------------------------------------ 1. login
PUB=""
for k in id_ed25519 id_ecdsa id_rsa; do [ -f "$HOME/.ssh/$k.pub" ] && { PUB="$HOME/.ssh/$k.pub"; break; }; done
[ -n "$PUB" ] && KEY_OPTS=(-i "${PUB%.pub}")

step "Connecting to $TARGET"
if err=$(ssh -o BatchMode=yes "${SSH_BASE[@]}" "${KEY_OPTS[@]}" "$TARGET" true 2>&1 >/dev/null); then
    KEY_OK=1; c_ok "SSH key login works"
elif printf '%s' "$err" | grep -qiE 'no route|timed out|connection refused|could not resolve|unreachable|host is down'; then
    c_err "cannot reach $TARGET: $err"
    c_err "(is the Pi on, on this network, and is the address right? try: ping ${TARGET#*@})"
    exit 1
else
    need_password || exit 1
    ssh_pw true || { c_err "cannot log in to $TARGET (wrong password?)"; exit 1; }
    c_ok "password login works"
    if [ "$NO_KEY" = 0 ]; then
        if [ -z "$PUB" ]; then
            mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
            ssh-keygen -q -t ed25519 -N "" -C "arstro-setup@$(hostname)" -f "$HOME/.ssh/id_ed25519"
            PUB="$HOME/.ssh/id_ed25519.pub"
        fi
        ssh_pw 'umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; k=$(cat); grep -qxF "$k" ~/.ssh/authorized_keys || echo "$k" >> ~/.ssh/authorized_keys' <"$PUB"
        KEY_OPTS=(-i "${PUB%.pub}")
        if key_works; then KEY_OK=1; c_ok "installed SSH key $PUB on the Pi (no password needed next time)"
        else c_warn "key installed but key login still fails; continuing with the password"; fi
    fi
fi

# ------------------------------------------------------------------ 2. probe
step "Checking the Pi"
PROBE=$(remote 'bash -s' <<'EOF'
echo "USER=$(id -un)"
echo "ARCH=$(uname -m)"
. /etc/os-release 2>/dev/null && echo "OS=$PRETTY_NAME"
echo "KERNEL=$(uname -r)"
echo "PY=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo none)"
echo "GROUPS=$(id -nG)"
echo "BT_ADAPTER=$(ls /sys/class/bluetooth 2>/dev/null | grep -v : | head -1)"
echo "BLUETOOTHD=$(systemctl is-active bluetooth 2>/dev/null || true)"
dm=/etc/systemd/system/display-manager.service
echo "DM=$([ -e "$dm" ] && basename "$(readlink -f "$dm")" .service || echo none)"
echo "AUTOLOGIN=$(grep -rhs '^autologin-user=' /usr/share/lightdm/lightdm.conf.d /etc/lightdm/lightdm.conf.d /etc/lightdm/lightdm.conf 2>/dev/null | cut -d= -f2 | tr '\n' ' ')"
echo "SEAT_SESSION=$(loginctl list-sessions --no-legend 2>/dev/null | awk -v u="$(id -un)" '$3==u && $4=="seat0"' | wc -l)"
echo "LAUNCHER=$(pgrep -u "$(id -u)" -f '^/bin/bash .*arstro-remote-launcher' >/dev/null && echo running || echo stopped)"
m=""
command -v python3 >/dev/null || m="$m python3"
python3 -c 'import dbus' 2>/dev/null || m="$m python3-dbus"
python3 -c 'from gi.repository import GLib' 2>/dev/null || m="$m python3-gi"
python3 -c 'import psutil' 2>/dev/null || m="$m python3-psutil"
command -v nmcli >/dev/null || m="$m network-manager"
command -v bluetoothctl >/dev/null || m="$m bluez"
python3 -c 'import ctypes.util,sys; sys.exit(not ctypes.util.find_library("Xtst"))' 2>/dev/null || m="$m libxtst6"
command -v xev >/dev/null || m="$m x11-utils"
# recorder (HDMI RX): GStreamer from Python, V4L2 tools, FFmpeg for the gallery
python3 -c 'import gi; gi.require_version("Gst", "1.0"); gi.require_version("GstVideo", "1.0"); gi.require_version("GstPbutils", "1.0")' 2>/dev/null ||
    m="$m python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0"
command -v gst-inspect-1.0 >/dev/null || m="$m gstreamer1.0-tools"
gst-inspect-1.0 capssetter >/dev/null 2>&1 || m="$m gstreamer1.0-plugins-good"
gst-inspect-1.0 h264parse >/dev/null 2>&1 || m="$m gstreamer1.0-plugins-bad"
gst-inspect-1.0 x264enc >/dev/null 2>&1 || m="$m gstreamer1.0-plugins-ugly"   # smooth live preview
gst-inspect-1.0 videoconvert >/dev/null 2>&1 || m="$m gstreamer1.0-plugins-base"
command -v v4l2-ctl >/dev/null || m="$m v4l-utils"
command -v ffprobe >/dev/null || m="$m ffmpeg"
echo "MISSING=${m# }"
echo "VPU=$(gst-inspect-1.0 mpph265enc >/dev/null 2>&1 && echo yes || echo no)"
hdmirx=""
for d in /sys/class/video4linux/video*; do
    [ -e "$d/name" ] && grep -qi hdmirx "$d/name" && hdmirx="/dev/$(basename "$d")" && break
done
echo "HDMIRX=$hdmirx"
EOF
)
get() { printf '%s\n' "$PROBE" | sed -n "s/^$1=//p" | head -1; }
BT_ADAPTER=$(get BT_ADAPTER)
MISSING=$(get MISSING)
HDMIRX_DEV=$(get HDMIRX)
printf '  %-10s %s\n' \
    "OS" "$(get OS) ($(get ARCH), kernel $(get KERNEL))" \
    "Python" "$(get PY)" \
    "Groups" "$(get GROUPS)" \
    "Bluetooth" "adapter: ${BT_ADAPTER:-NONE}, bluetoothd: $(get BLUETOOTHD)" \
    "Desktop" "$(get DM), auto-login: $(get AUTOLOGIN)" \
    "Packages" "${MISSING:-all present}" \
    "HDMI RX" "${HDMIRX_DEV:-not found}" \
    "VPU" "$(get VPU) (Rockchip MPP GStreamer encoders)" \
    "Service" "$(get LAUNCHER)"
[ -n "$BT_ADAPTER" ] || c_warn "WARNING: no Bluetooth adapter found (/sys/class/bluetooth is empty) - check the Wi-Fi/BT card and driver"
[ -n "$(get HDMIRX)" ] || c_warn "WARNING: no HDMI RX capture device - enable it with server/scripts/setup_hdmirx.sh on the Pi (needs sudo + reboot)"
[ "$(get VPU)" = yes ] || c_warn "WARNING: no Rockchip MPP encoders (mpph265enc): H.265 recording and the H.264 share copy are unavailable; the preview falls back to x264"

# ------------------------------------------------------------------ 3. system
NEED_RELOGIN=0
if [ "$NO_DEPS" = 0 ]; then
    if [ -n "$MISSING" ]; then
        step "Installing packages: $MISSING"
        if rsudo "sh -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $MISSING' >/tmp/arstro-apt.log 2>&1"; then
            c_ok "packages installed"
        else
            remote 'tail -15 /tmp/arstro-apt.log' >&2 || true
            c_err "apt-get failed (full log on the Pi: /tmp/arstro-apt.log)"; exit 1
        fi
    fi
    add=""
    for g in bluetooth netdev; do
        if remote "getent group $g >/dev/null" && ! [[ " $(get GROUPS) " == *" $g "* ]]; then add="$add${add:+,}$g"; fi
    done
    if [ -n "$add" ]; then
        step "Adding $PI_USER to groups: $add"
        if rsudo "usermod -aG $add $PI_USER"; then NEED_RELOGIN=1; else c_warn "could not add groups"; fi
    fi
    case "$(get BLUETOOTHD)" in
        active) ;;
        inactive|failed|activating)
            step "Enabling the Bluetooth service"
            rsudo "systemctl enable --now bluetooth" || c_warn "could not enable bluetooth.service" ;;
        *) c_warn "cannot tell whether bluetoothd runs (no systemd?)" ;;
    esac
elif [ -n "$MISSING" ]; then
    c_warn "missing packages ($MISSING) - install.sh will refuse to run; drop --no-deps or: sudo apt install $MISSING"
fi

if [[ " $(get AUTOLOGIN) " != *" $PI_USER "* ]]; then
    if [ "$AUTOLOGIN" = 1 ]; then
        if [ "$(get DM)" = lightdm ]; then
            step "Enabling LightDM auto-login for $PI_USER"
            printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$PI_USER" | remote 'cat > ~/.arstro-autologin.conf'
            rsudo "sh -c 'mkdir -p /etc/lightdm/lightdm.conf.d && install -m 644 ~$PI_USER/.arstro-autologin.conf /etc/lightdm/lightdm.conf.d/60-arstro-autologin.conf'"
            remote 'rm -f ~/.arstro-autologin.conf'
            NEED_RELOGIN=1
            c_ok "auto-login configured (takes effect at the next boot)"
        else
            c_warn "display manager is '$(get DM)', not LightDM - enable auto-login for $PI_USER by hand"
        fi
    else
        c_warn "desktop auto-login is not set for $PI_USER: the service starts only after someone logs in on the Pi (use --autologin)"
    fi
fi

# ------------------------------------------------------------------ 4. install
step "Copying server/ and running install.sh"
COPYFILE_DISABLE=1 tar -C "$ROOT/server" --exclude='__pycache__' --exclude='*.pyc' --exclude='node_modules' -czf - . | \
    remote 'rm -rf ~/arstro-remote-src && mkdir -p ~/arstro-remote-src && tar -xzf - -C ~/arstro-remote-src'
# IO Control needs udev rules and groups (root): done here with the Pi's sudo password
rsudo "bash ~$PI_USER/arstro-remote-src/scripts/setup_io_perms.sh $PI_USER" | sed 's/^/  /' || c_warn "IO device permissions not set up"
remote "cd ~/arstro-remote-src && ./install.sh --slot $SLOT --yes --no-deps --no-perms" | sed 's/^/  /'

# the newest APK, offered at http://<pi>:<port>/app.apk for phones without a cable (SET-04)
apk=$(ls -t "$ROOT"/release/arstro-remote-v*.apk 2>/dev/null | head -1 || true)
if [ -n "$apk" ]; then
    ver=$(basename "$apk" .apk | sed 's/^arstro-remote-v//')
    remote 'd=${XDG_DATA_HOME:-$HOME/.local/share}/arstro-remote/app; mkdir -p "$d" && cat > "$d/arstro-remote.apk.part" && mv -f "$d/arstro-remote.apk.part" "$d/arstro-remote.apk"' <"$apk"
    printf '%s\n' "$ver" | remote 'd=${XDG_DATA_HOME:-$HOME/.local/share}/arstro-remote/app; cat > "$d/version"'
    c_ok "app v$ver uploaded: phones on the same network can install it from http://${TARGET#*@}:$WEB_PORT/app.apk"
fi

# ------------------------------------------------------------------ 5. start / verify
wait_status() {  # $1 = seconds
    local end=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "$end" ]; do
        if remote "$CLI status" >"$STATUS_FILE" 2>/dev/null; then return 0; fi
        sleep 3
    done
    return 1
}
running=0
if [ "$(get LAUNCHER)" = running ] && [ "$NEED_RELOGIN" = 0 ]; then
    step "Waiting for the restarted service"
    wait_status 30 && running=1
fi
if [ "$running" = 0 ] && [ "$REBOOT" = 1 ]; then
    step "Rebooting the Pi"
    rsudo "systemctl --no-block reboot" || true
    sleep 20
    end=$(( $(date +%s) + 300 ))
    until remote true 2>/dev/null; do
        [ "$(date +%s)" -lt "$end" ] || { c_err "the Pi did not come back within 5 minutes"; exit 1; }
        sleep 5
    done
    c_ok "Pi is back, waiting for the desktop session to start the service"
    wait_status 150 && running=1
fi

if [ "$running" = 1 ]; then
    step "Service status"
    sed 's/^/  /' "$STATUS_FILE"
else
    c_warn "The service is installed but not running yet."
    c_warn "It starts with $PI_USER's desktop session: reboot the Pi (or run again with --reboot)."
    [ "$NEED_RELOGIN" = 1 ] && c_warn "(groups / auto-login changed: a reboot is required anyway)"
fi

if [ "$WEB_PASS_SET" = 1 ]; then
    if [ "$running" = 1 ]; then
        if [ -z "$WEB_PASS" ]; then
            ask WEB_PASS "Web / remote-CLI password (8+ characters): " silent || { c_err "no terminal to ask - set ARSTRO_WEB_PASS"; exit 1; }
        fi
        step "Setting the web password"
        printf '%s\n' "$WEB_PASS" | remote "$CLI web --set-password -" | sed 's/^/  /'
    else
        c_warn "cannot set the web password yet: the service is not running (run again after a reboot)"
    fi
fi

if [ "$RUN_TESTS" = 1 ]; then
    if [ "$running" = 1 ]; then
        step "Running the Pi test suites"
        remote 'cd ~/arstro-remote-src/tests && export DISPLAY=:0 XAUTHORITY=~/.Xauthority;
                for t in test_daemon.py test_sync_web.py test_recorder.py; do echo "-- $t"; python3 $t 2>&1 | grep -E "^(PASS|FAIL)|passed"; done' | \
            sed 's/^/  /'
    else
        c_warn "skipping tests: the service is not running"
    fi
fi

if [ "$APK" = 1 ]; then
    step "Installing the Android app"
    apk=$(ls -t "$ROOT"/release/*.apk 2>/dev/null | head -1)
    if [ -z "$apk" ]; then c_err "no APK in release/ (run ./build_apk.sh)";
    elif ! command -v adb >/dev/null; then c_err "adb not found";
    else adb ${APK_SERIAL:+-s "$APK_SERIAL"} install -r "$apk" | tail -1; fi
fi

step "Done"
cat <<EOF
  Pi:        $TARGET  (key login: $([ "$KEY_OK" = 1 ] && echo yes || echo no))
  Manage:    ssh $([ "$PORT" = 22 ] || echo "-p $PORT ")$TARGET '$CLI status'   (pair | unpair ADDR | rec | gallery | web)
  Web:       http://${TARGET#*@}:$WEB_PORT/   (slot ${SLOT^^}; password: 'arstro-remote-$SLOT web --show' on the Pi)
  Phone:     install release/*.apk, open Arstro Remote, Scan, pick "Arstro-<hostname>".
             New phones can pair during the first 10 min after boot or after 'arstro-remote pair'.
EOF
