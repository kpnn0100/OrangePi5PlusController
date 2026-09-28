#!/bin/bash
# Install Arstro Remote on the Orange Pi for the current (desktop) user. No root needed.
#
#   ./install.sh            install / update and (re)start the service
#   ./install.sh --no-start install files only
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/arstro-remote"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/arstro-remote"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/arstro-remote.desktop"
START=1
[ "${1:-}" = "--no-start" ] && START=0

if [ "$(id -u)" = 0 ]; then
    echo "Run this as the desktop user (the auto-login user), not root." >&2
    exit 1
fi

echo "==> Checking dependencies"
missing=()
python3 -c 'import dbus' 2>/dev/null || missing+=(python3-dbus)
python3 -c 'from gi.repository import GLib' 2>/dev/null || missing+=(python3-gi)
python3 -c 'import psutil' 2>/dev/null || missing+=(python3-psutil)
command -v nmcli >/dev/null || missing+=(network-manager)
command -v bluetoothctl >/dev/null || missing+=(bluez)
python3 -c 'import ctypes.util,sys; sys.exit(not ctypes.util.find_library("Xtst"))' || missing+=(libxtst6)
python3 -c 'import socket; socket.AF_BLUETOOTH' 2>/dev/null || missing+=("python3 with Bluetooth sockets")
if [ ${#missing[@]} -gt 0 ]; then
    echo "Missing: ${missing[*]}"
    echo "Install with: sudo apt install ${missing[*]}"
    exit 1
fi
id -nG | grep -qw bluetooth || echo "WARNING: $(id -un) is not in group 'bluetooth'"
id -nG | grep -qw netdev || echo "WARNING: $(id -un) is not in group 'netdev' (needed for rfkill)"

echo "==> Installing to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$CFG_DIR" "$(dirname "$AUTOSTART")"
rm -rf "$APP_DIR/arstro_remote"
cp -r "$SRC/arstro_remote" "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
install -m 755 "$SRC/scripts/arstro-remote-launcher" "$BIN_DIR/arstro-remote-launcher"
cat >"$BIN_DIR/arstro-remote" <<EOF
#!/bin/sh
export PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m arstro_remote "\$@"
EOF
chmod 755 "$BIN_DIR/arstro-remote"

if [ ! -f "$CFG_DIR/config.json" ]; then
    cat >"$CFG_DIR/config.json" <<'EOF'
{
  "alias": "Arstro-{hostname}",
  "pairing": "window",
  "pair_window_sec": 600,
  "channel": 22
}
EOF
    echo "==> Wrote default config $CFG_DIR/config.json"
fi

cat >"$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Arstro Remote
Comment=Bluetooth remote control service (Arstro Remote System)
Exec=$BIN_DIR/arstro-remote-launcher
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
EOF
echo "==> Autostart entry $AUTOSTART"

if [ "$START" = 1 ]; then
    if pgrep -u "$(id -u)" -f '^/bin/bash .*arstro-remote-launcher' >/dev/null; then
        echo "==> Restarting daemon (the running launcher picks up the new code)"
        pkill -u "$(id -u)" -f '^python3 -m arstro_remote run' || true
    elif [ -n "${DISPLAY:-}" ] && [ -z "${SSH_CONNECTION:-}" ]; then
        echo "==> Starting launcher in this desktop session"
        setsid "$BIN_DIR/arstro-remote-launcher" >/dev/null 2>&1 < /dev/null &
    else
        echo "==> Installed. The service starts with the desktop session:"
        echo "    reboot (autologin) or log out/in on the Pi screen."
        echo "    (Started from SSH it would lack polkit rights for Wi-Fi control.)"
    fi
fi
echo "Done. Check with: $BIN_DIR/arstro-remote status"
