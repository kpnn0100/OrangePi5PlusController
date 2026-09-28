#!/usr/bin/env bash
# One-time setup: enable the RK3588 HDMI RX port on Orange Pi 5 Plus.
#   - enables the rk3588-hdmirx device-tree overlay
#   - raises CMA so 4K frame buffers (~25 MB each) can be allocated
#   - installs gstreamer1.0-libav (FFV1 lossless encoder for the recorder)
# Run with: sudo ./setup_hdmirx.sh   then reboot.
set -euo pipefail

ENV=/boot/orangepiEnv.txt
CMA=${CMA:-512M}

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0"; exit 1; }
[ -f "$ENV" ] || { echo "$ENV not found"; exit 1; }
[ -f /boot/dtb/rockchip/overlay/rk3588-hdmirx.dtbo ] || { echo "rk3588-hdmirx.dtbo not found"; exit 1; }

cp -n "$ENV" "$ENV.bak-hdmirx"

# overlays=... (append hdmirx if missing)
if grep -q '^overlays=' "$ENV"; then
    grep -qE '^overlays=.*\bhdmirx\b' "$ENV" || sed -i 's/^overlays=\(.*\)$/overlays=\1 hdmirx/' "$ENV"
else
    echo 'overlays=hdmirx' >> "$ENV"
fi

# extraargs=... cma=<CMA>
if grep -q '^extraargs=' "$ENV"; then
    if grep -qE '^extraargs=.*\bcma=' "$ENV"; then
        sed -i -E "s/^(extraargs=.*)\bcma=[^ ]*/\1cma=$CMA/" "$ENV"
    else
        sed -i "s/^extraargs=\(.*\)$/extraargs=\1 cma=$CMA/" "$ENV"
    fi
else
    echo "extraargs=cma=$CMA" >> "$ENV"
fi

echo "Updated $ENV (backup: $ENV.bak-hdmirx):"
grep -E '^(overlays|extraargs)=' "$ENV"

# FFV1 encoder (avenc_ffv1) used by the recorder's lossless copies
if ! dpkg -s gstreamer1.0-libav >/dev/null 2>&1; then
    apt-get install -y gstreamer1.0-libav || echo "warning: could not install gstreamer1.0-libav (FFV1 copies disabled)"
fi

echo
echo "Reboot now, then run: python3 hdmi_recorder.py   (or hdmi_rx_viewer.py for a plain viewer)"
