#!/usr/bin/env bash
# Smoke test of the setup and build scripts (runs anywhere, touches nothing).
# covers: SET-01, SET-02, SET-03, SET-06
#
#   scripts/check_scripts.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok()  { printf 'PASS  %s\n' "$*"; }
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }

for s in setup_all.sh build_apk.sh install.sh scripts/setup_all.sh scripts/setup_pi.sh scripts/build_apk.sh \
         scripts/dev_tunnel.sh server/install.sh server/uninstall.sh server/scripts/arstro-remote-launcher \
         server/scripts/install_deps.sh server/scripts/setup_io_perms.sh \
         server/scripts/setup_hdmirx.sh server/scripts/setup_gpu.sh; do
    [ -f "$ROOT/$s" ] || { bad "$s missing"; continue; }
    [ -x "$ROOT/$s" ] || bad "$s is not executable"
    bash -n "$ROOT/$s" && ok "$s syntax" || bad "$s syntax"
done
for s in setup_all.sh scripts/setup_pi.sh scripts/build_apk.sh; do
    out=$("$ROOT/$s" --help 2>&1) && [ -n "$out" ] && ok "$s --help" || bad "$s --help"
done
# setup_all without any work to do only checks the tools and prints the summary
"$ROOT/scripts/setup_all.sh" --no-pi --no-apk >/dev/null 2>&1 && ok "setup_all.sh --no-pi --no-apk" || bad "setup_all.sh --no-pi --no-apk"
# the scripts find their files from any working directory
(cd /tmp && "$ROOT/scripts/build_apk.sh" --help >/dev/null 2>&1) && ok "build_apk.sh from another directory" || bad "build_apk.sh from another directory"
grep -q 'ROOT/server' "$ROOT/scripts/setup_pi.sh" && ok "setup_pi.sh installs server/" || bad "setup_pi.sh does not copy server/"

# covers: ADM-06, SET-05 - install.sh into a throwaway home: a slot with its own dirs, port,
# password, modules and autostart entry; uninstall removes it again
t=$(mktemp -d)
if HOME="$t" XDG_CONFIG_HOME="$t/cfg" XDG_DATA_HOME="$t/data" XDG_STATE_HOME="$t/state" XDG_RUNTIME_DIR="$t/run" \
   DISPLAY=:99 "$ROOT/install.sh" --slot y --no-deps --no-perms --no-start --boot desktop --yes \
   --modules monitor,terminal,files --password testpass1 >"$t/out" 2>&1; then
    c="$t/cfg/arstro-remote-y"
    [ -f "$t/data/arstro-remote-y/arstro_remote/daemon.py" ] && [ -x "$t/.local/bin/arstro-remote-y" ] &&
    [ "$(cat "$c/web_token")" = testpass1 ] && [ "$(stat -c %a "$c/web_token")" = 600 ] &&
    python3 -c "import json,sys; c=json.load(open('$c/config.json')); sys.exit(not (c['web_port']==8104 and c['modules']==['monitor','terminal','files'] and c['bluetooth_enabled'] is True))" &&
    [ "$(readlink "$t/.local/bin/arstro-remote")" = arstro-remote-y ] &&
    grep -q "ARSTRO_SLOT=y" "$t/cfg/autostart/arstro-remote-y.desktop" &&
        ok "install.sh --slot y (dirs, port 8104, password, modules, autostart; first slot = active)" || { bad "install.sh --slot y result"; cat "$t/out"; }
    HOME="$t" XDG_CONFIG_HOME="$t/cfg" XDG_DATA_HOME="$t/data" XDG_STATE_HOME="$t/state" XDG_RUNTIME_DIR="$t/run" \
        "$ROOT/server/uninstall.sh" --slot y --purge >/dev/null 2>&1
    [ ! -e "$t/data/arstro-remote-y" ] && [ ! -e "$t/cfg/autostart/arstro-remote-y.desktop" ] && [ ! -e "$c" ] &&
        ok "uninstall.sh --slot y --purge" || bad "uninstall.sh --slot y left files"
else
    bad "install.sh --slot y failed"; cat "$t/out"
fi
rm -rf "$t"
out=$("$ROOT/server/install.sh" --slot 1 2>&1) && bad "install.sh accepted a bad slot" || ok "install.sh rejects a bad slot"
# SET-06: setup_pi.sh deploys into a slot (a bad one is refused before any SSH)
"$ROOT/scripts/setup_pi.sh" --slot 1 nobody@invalid.invalid >/dev/null 2>&1; [ $? = 2 ] &&
    grep -q -- '--slot $SLOT' "$ROOT/scripts/setup_pi.sh" && ok "setup_pi.sh --slot" || bad "setup_pi.sh --slot"
grep -q 'keytool -genkeypair' "$ROOT/scripts/build_apk.sh" && ok "build_apk.sh creates a signing key" || bad "build_apk.sh signing key"
exit $fail
