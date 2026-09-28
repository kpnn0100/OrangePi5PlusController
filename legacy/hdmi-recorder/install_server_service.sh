#!/usr/bin/env bash
# Install (or remove) the recordings web server as a boot service.
#   sudo ./install_server_service.sh            # install + start (port 8000)
#   sudo ./install_server_service.sh uninstall
# Other computers then open http://<board-ip>:8000/ to browse and download recordings.
set -euo pipefail

NAME=hdmi-recorder-server
UNIT=/etc/systemd/system/$NAME.service
PORT=${PORT:-8000}
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER=${SUDO_USER:-$(stat -c %U "$DIR")}

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0"; exit 1; }

if [ "${1:-}" = "uninstall" ]; then
    systemctl disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "Removed $NAME."
    exit 0
fi

cat > "$UNIT" <<UNIT
[Unit]
Description=HDMI Recorder - recordings web server on port $PORT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$(id -gn "$RUN_USER")
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $DIR/hdmi_recorder.py serve --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3
# downloads must never slow down a recording's disk writes
Nice=5
IOSchedulingClass=best-effort
IOSchedulingPriority=7

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "$NAME"
sleep 2
systemctl --no-pager --lines=3 status "$NAME" || true
echo
echo "Recordings: http://$(hostname -I | awk '{print $1}'):$PORT/   (starts at every boot)"
