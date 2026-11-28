#!/usr/bin/env bash
# The RK3588 HDMI RX port on Orange Pi images (camera module) - run as root; install.sh calls it
# through sudo when the overlay or the CMA size is missing. Takes effect after a reboot.
#   - enables the rk3588-hdmirx device-tree overlay
#   - sets CMA (when the image sets none) so 4K frame buffers (~25 MB each) can be allocated
#
#   setup_hdmirx.sh [--check]     --check: exit 0 when nothing needs doing (no root needed)
set -euo pipefail
# Part of install.sh (SET-08): it refuses to run on its own - set the machine up with ./install.sh.
[ "${1:-}" = --from-install-sh ] && shift || { echo "$(basename "$0") is part of the installer - run ./install.sh instead (./install.sh --help)" >&2; exit 2; }

ENV=/boot/orangepiEnv.txt
CMA=${CMA:-512M}

if [ "${1:-}" = --check ]; then     # nothing to do: not an Orange Pi image, no overlay, or both set already
    [ -f "$ENV" ] && [ -f /boot/dtb/rockchip/overlay/rk3588-hdmirx.dtbo ] || exit 0
    grep -qE '^overlays=.*\bhdmirx\b' "$ENV" && grep -qE "^extraargs=.*\bcma=" "$ENV"
    exit $?
fi
[ "$(id -u)" -eq 0 ] || { echo "setup_hdmirx.sh must run as root" >&2; exit 1; }
[ -f "$ENV" ] || { echo "$ENV not found"; exit 1; }
[ -f /boot/dtb/rockchip/overlay/rk3588-hdmirx.dtbo ] || { echo "rk3588-hdmirx.dtbo not found"; exit 1; }

cp -n "$ENV" "$ENV.bak-hdmirx"

# overlays=... (append hdmirx if missing)
if grep -q '^overlays=' "$ENV"; then
    grep -qE '^overlays=.*\bhdmirx\b' "$ENV" || sed -i 's/^overlays=\(.*\)$/overlays=\1 hdmirx/' "$ENV"
else
    echo 'overlays=hdmirx' >> "$ENV"
fi

# extraargs=... cma=<CMA> (a cma= that is set already stays as it is)
if grep -q '^extraargs=' "$ENV"; then
    grep -qE '^extraargs=.*\bcma=' "$ENV" || sed -i "s/^extraargs=\(.*\)$/extraargs=\1 cma=$CMA/" "$ENV"
else
    echo "extraargs=cma=$CMA" >> "$ENV"
fi

echo "Updated $ENV (backup: $ENV.bak-hdmirx):"
grep -E '^(overlays|extraargs)=' "$ENV"

echo "HDMI RX: takes effect after a reboot."
