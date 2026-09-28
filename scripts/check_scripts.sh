#!/usr/bin/env bash
# Smoke test of the setup and build scripts (runs anywhere, touches nothing).
# covers: SET-01, SET-02, SET-03
#
#   scripts/check_scripts.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
ok()  { printf 'PASS  %s\n' "$*"; }
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }

for s in setup_all.sh build_apk.sh scripts/setup_all.sh scripts/setup_pi.sh scripts/build_apk.sh \
         scripts/dev_tunnel.sh server/install.sh server/uninstall.sh server/scripts/arstro-remote-launcher \
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
grep -q 'keytool -genkeypair' "$ROOT/scripts/build_apk.sh" && ok "build_apk.sh creates a signing key" || bad "build_apk.sh signing key"
exit $fail
