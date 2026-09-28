#!/bin/bash
# Remove Arstro Remote for the current user (config and logs are kept unless --purge).
set -u
pkill -u "$(id -u)" -f '^/bin/bash .*arstro-remote-launcher' 2>/dev/null
pkill -u "$(id -u)" -f '^python3 -m arstro_remote run' 2>/dev/null
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/arstro-remote.desktop" \
      "$HOME/.local/bin/arstro-remote" "$HOME/.local/bin/arstro-remote-launcher"
rm -rf "$HOME/.local/share/arstro-remote"
if [ "${1:-}" = "--purge" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/arstro-remote" "${XDG_STATE_HOME:-$HOME/.local/state}/arstro-remote"
fi
echo "Arstro Remote removed."
