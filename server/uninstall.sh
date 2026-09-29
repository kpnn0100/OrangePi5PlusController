#!/bin/bash
# Remove Arstro Remote for the current user (config and logs are kept unless --purge).
#
#   uninstall.sh --slot b [--purge]    one slot (same as install.sh --slot b --uninstall)
#   uninstall.sh --legacy [--purge]    an old, unslotted install (arstro-remote.desktop)
set -u
if [ "${1:-}" != "--legacy" ]; then
    exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --uninstall "$@"
fi
shift
pkill -u "$(id -u)" -f '^/bin/bash .*/\.local/bin/arstro-remote-launcher' 2>/dev/null
pid=$(head -c 20 "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/arstro-remote.lock" 2>/dev/null | tr -dc 0-9)
[ -n "$pid" ] && kill "$pid" 2>/dev/null
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/arstro-remote.desktop" "$HOME/.local/bin/arstro-remote-launcher"
[ -L "$HOME/.local/bin/arstro-remote" ] || rm -f "$HOME/.local/bin/arstro-remote"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/arstro-remote/arstro_remote"
if [ "${1:-}" = "--purge" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/arstro-remote" "${XDG_STATE_HOME:-$HOME/.local/state}/arstro-remote"
fi
echo "Old unslotted Arstro Remote removed."
