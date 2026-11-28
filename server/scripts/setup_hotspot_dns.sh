#!/bin/bash
# The hotspot next to a dnsmasq service (WIFI-08) - run as root (install.sh calls it through sudo).
#
# NetworkManager hands out the hotspot's addresses with its own dnsmasq on 10.42.0.1:53. A dnsmasq
# *service* with the stock configuration listens on every address, holds port 53, and NetworkManager's
# dnsmasq cannot start: the hotspot goes down the moment it comes up ("address in use"). This drop-in
# makes the service bind address by address and leave the Wi-Fi interfaces (wl*) to NetworkManager; it
# keeps serving every other interface. Nothing else changes; delete the file to undo.
#
#   setup_hotspot_dns.sh [--check]     --check: exit 0 when nothing needs doing
set -u
# Part of install.sh (SET-08): it refuses to run on its own - set the machine up with ./install.sh.
[ "${1:-}" = --from-install-sh ] && shift || { echo "$(basename "$0") is part of the installer - run ./install.sh instead (./install.sh --help)" >&2; exit 2; }
CONF=/etc/dnsmasq.d/arstro-hotspot.conf
WANT="arstro-hotspot-dns v1"

if ! systemctl cat dnsmasq.service >/dev/null 2>&1; then
    [ "${1:-}" = --check ] || echo "no dnsmasq service on this machine - nothing to do"
    exit 0
fi
if grep -qs "$WANT" "$CONF"; then
    [ "${1:-}" = --check ] || echo "already set up ($CONF)"
    exit 0
fi
[ "${1:-}" = --check ] && exit 1
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

# bind-dynamic only when the configuration does not choose a bind mode itself (both together are an error)
bind="bind-dynamic"
if grep -Eqs '^\s*bind-(interfaces|dynamic)' /etc/dnsmasq.conf /etc/dnsmasq.d/*; then bind="# (bind mode: set elsewhere)"; fi
mkdir -p /etc/dnsmasq.d
cat >"$CONF.tmp" <<CONF
# $WANT - Arstro Remote (WIFI-08): NetworkManager runs its own dnsmasq for the Wi-Fi hotspot.
# This service binds address by address and leaves the Wi-Fi interfaces to it. Delete to undo.
$bind
except-interface=wl*
CONF
mv "$CONF.tmp" "$CONF"
chmod 644 "$CONF"
if ! dnsmasq --test >/dev/null 2>&1; then
    rm -f "$CONF"
    echo "dnsmasq does not accept the change - left as it was" >&2
    exit 1
fi
systemctl try-restart dnsmasq.service
echo "the dnsmasq service now leaves the Wi-Fi interfaces to the hotspot ($CONF)"
