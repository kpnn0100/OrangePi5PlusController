#!/usr/bin/env bash
# Smoke test of the setup and build scripts (runs anywhere, touches nothing).
# covers: SET-03, SET-05, SET-07, SET-08
#
#   scripts/check_scripts.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok()  { printf 'PASS  %s\n' "$*"; }
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }

for s in build_apk.sh install.sh scripts/build_apk.sh \
         scripts/setup_android_dev.sh \
         scripts/dev_tunnel.sh server/install.sh server/uninstall.sh server/scripts/arstro-remote-launcher \
         server/scripts/install_deps.sh server/scripts/setup_io_perms.sh server/scripts/setup_hotspot_dns.sh \
         server/scripts/setup_hdmirx.sh server/scripts/setup_gpu.sh server/scripts/install_backup.sh \
         relay/package.sh relay/kit/run.sh \
         scripts/preview.sh scripts/bundle.sh; do
    [ -f "$ROOT/$s" ] || { bad "$s missing"; continue; }
    [ -x "$ROOT/$s" ] || bad "$s is not executable"
    bash -n "$ROOT/$s" && ok "$s syntax" || bad "$s syntax"
done
for s in install.sh scripts/build_apk.sh scripts/setup_android_dev.sh; do
    out=$("$ROOT/$s" --help 2>&1) && [ -n "$out" ] && ok "$s --help" || bad "$s --help"
done
"$ROOT/relay/package.sh" --help | grep -q arm64-host && ok "relay/package.sh --help" || bad "relay/package.sh --help"
"$ROOT/relay/kit/run.sh" --help | grep -q token && ok "relay/kit/run.sh --help" || bad "relay/kit/run.sh --help"
"$ROOT/scripts/bundle.sh" --help | grep -q device-manager && ok "scripts/bundle.sh --help" || bad "scripts/bundle.sh --help"
"$ROOT/scripts/preview.sh" --help | grep -q 'stop' && ok "scripts/preview.sh --help" || bad "scripts/preview.sh --help"
# covers: RLY-20
# the scripts find their files from any working directory
(cd /tmp && "$ROOT/scripts/build_apk.sh" --help >/dev/null 2>&1) && ok "build_apk.sh from another directory" || bad "build_apk.sh from another directory"
# SET-08: install.sh is the only script that sets a machine up - its parts refuse to run alone,
# and no other setup script exists
for s in install_deps.sh setup_io_perms.sh setup_hotspot_dns.sh setup_hdmirx.sh setup_gpu.sh install_backup.sh; do
    out=$(bash "$ROOT/server/scripts/$s" --check root 2>&1); rc=$?
    [ "$rc" = 2 ] && [[ "$out" == *"run ./install.sh"* ]] && ok "$s refuses to run without install.sh" || bad "$s ran on its own ($rc: $out)"
    grep -qF -- "$s\" --from-install-sh" "$ROOT/server/install.sh" && ok "install.sh runs $s" || bad "install.sh does not run $s"
done
others=$(cd "$ROOT" && git ls-files '*.sh' | grep -iE '(^|/)(setup_(pi|all|gpu|hdmirx)|install[^/]*)\.sh$' |
         grep -vxE 'install\.sh|server/install\.sh|server/scripts/(install_deps|install_backup|setup_gpu|setup_hdmirx)\.sh')
[ -z "$others" ] && ok "no other machine setup script in the repo" || bad "other setup scripts: $others"
help=$("$ROOT/install.sh" --help)
for o in --autologin --no-gpu --slot --backup --modules; do
    [[ "$help" == *"$o"* ]] || bad "install.sh --help lacks $o"
done
ok "install.sh --help lists --autologin, --no-gpu, --slot, --backup, --modules"

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
# covers: RLY-08, RLY-02 - install.sh --relay: relay.json (0600, on), the identity (0600) and the
# access key printed at the end; --relay without its token is refused
t=$(mktemp -d)
if HOME="$t" XDG_CONFIG_HOME="$t/cfg" XDG_DATA_HOME="$t/data" XDG_STATE_HOME="$t/state" XDG_RUNTIME_DIR="$t/run" \
   DISPLAY=:99 "$ROOT/install.sh" --slot x --no-deps --no-perms --no-start --boot desktop --yes \
   --modules system,relay --password testpass1 --relay https://devices.example.com/arstro/internal/devices/ \
   --relay-token join-test-token --relay-name "Test box" >"$t/out" 2>&1; then
    c="$t/cfg/arstro-remote-x"
    key=$(sed -n 's/^Internet access key (give it only to people who may use this machine): //p' "$t/out")
    [ "$(stat -c %a "$c/relay.json")" = 600 ] && [ "$(stat -c %a "$c/relay_identity.json")" = 600 ] &&
    python3 -c "import json,sys; r=json.load(open('$c/relay.json')); i=json.load(open('$c/relay_identity.json')); sys.exit(not (r['url']=='https://devices.example.com/arstro/internal/devices' and r['token']=='join-test-token' and r['enabled'] is True and r['name']=='Test box' and i['key']=='$key' and '$key'.startswith('ak1_')))" &&
        ok "install.sh --relay (relay.json, identity, key printed)" || { bad "install.sh --relay result"; cat "$t/out"; }
    HOME="$t" XDG_CONFIG_HOME="$t/cfg" XDG_DATA_HOME="$t/data" XDG_STATE_HOME="$t/state" XDG_RUNTIME_DIR="$t/run" \
        "$ROOT/server/uninstall.sh" --slot x --purge >/dev/null 2>&1
else
    bad "install.sh --relay failed"; cat "$t/out"
fi
out=$(HOME="$t" XDG_CONFIG_HOME="$t/cfg" XDG_DATA_HOME="$t/data" XDG_STATE_HOME="$t/state" XDG_RUNTIME_DIR="$t/run" \
      "$ROOT/server/install.sh" --slot x --no-deps --no-perms --no-start --yes \
      --relay https://devices.example.com/x 2>&1) &&
    bad "install.sh accepted --relay without --relay-token" || ok "install.sh refuses --relay without --relay-token"
[ ! -e "$t/data/arstro-remote-x" ] || bad "install.sh --relay without a token installed anyway"
rm -rf "$t"
out=$("$ROOT/server/install.sh" --slot 1 2>&1) && bad "install.sh accepted a bad slot" || ok "install.sh rejects a bad slot"
grep -q 'keytool -genkeypair' "$ROOT/scripts/build_apk.sh" && ok "build_apk.sh creates a signing key" || bad "build_apk.sh signing key"
# SET-07: on ARM64 the x86-64-only build tools get qemu wrappers, refreshed before every build
grep -q 'linux-arm64/gen_snapshot' "$ROOT/scripts/setup_android_dev.sh" &&
    grep -q 'aapt2FromMavenOverride' "$ROOT/scripts/setup_android_dev.sh" &&
    grep -q 'setup_android_dev.sh" --shims-only' "$ROOT/scripts/build_apk.sh" &&
    ok "setup_android_dev.sh ARM64 wrappers" || bad "setup_android_dev.sh ARM64 wrappers"
exit $fail
