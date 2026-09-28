#!/bin/bash
# Real-radio pairing/authorization policy test, run from a Linux box with Bluetooth
# that can reach the Pi (e.g. the dev host):
#
#   server/tests/bt_pairing_security_test.sh <pi-bt-address> <pi-ssh-target>
#
# Checks: untrusted bonded device refused while the window is closed, pairing
# refused while closed, pairing + trust while open, trusted device still allowed
# after the window closes. Removes the test pairing on both sides at the end.
set -u
PI_BT=${1:?usage: $0 <pi-bt-address> <user@pi>}
PI_SSH=${2:?usage: $0 <pi-bt-address> <user@pi>}
HERE="$(cd "$(dirname "$0")" && pwd)"
MY_BT=$(bluetoothctl show | awk '/Controller/ {print $2; exit}')
pass=0; fail=0
ok()   { echo "PASS  $*"; pass=$((pass+1)); }
bad()  { echo "FAIL  $*"; fail=$((fail+1)); }
pi()   { ssh -o BatchMode=yes "$PI_SSH" "$@"; }

btctl() {  # feed bluetoothctl a timed script: btctl "cmd1" 2 "cmd2" 5 ...
    { echo "agent NoInputNoOutput"; echo "default-agent"; sleep 1
      while [ $# -gt 0 ]; do echo "$1"; sleep "${2:-1}"; shift 2 || shift; done
      echo "quit"; } | bluetoothctl 2>&1
}

discover() {  # scan until the host has the Pi cached (radio links can be flaky)
    for i in 1 2 3 4; do
        btctl "scan on" 20 "scan off" 1 >/dev/null
        bluetoothctl info "$PI_BT" >/dev/null 2>&1 && return 0
        echo "      (scan $i did not see the Pi, retrying)"
    done
    return 1
}

try_session() {  # prints PASS/FAIL line of a short protocol session
    timeout 120 python3 - "$PI_BT" "$HERE/.." <<'EOF'
import socket, sys, time
sys.path.insert(0, sys.argv[2])
from arstro_remote.client import Client
last = None
for i in range(6):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(15)
    try:
        s.connect((sys.argv[1], 22)); s.settimeout(None)
        c = Client(s)
        h = c.call("hello", app="security-test", timeout=10)
        print("SESSION_OK", h["hostname"]); c.close(); sys.exit(0)
    except (OSError, TimeoutError, RuntimeError) as e:
        last = e; s.close()
        if "Host is down" not in str(e):
            break          # refused/reset: do not retry
        time.sleep(2)
print("SESSION_FAILED", last)
EOF
}

echo "== host $MY_BT -> Pi $PI_BT"
pi '~/.local/bin/arstro-remote pair 0 >/dev/null'
st=$(pi '~/.local/bin/arstro-remote status')
echo "$st" | grep -q "Pairable: False" && echo "$st" | grep -q "Discoverable: False" \
    && ok "window closed -> not pairable, not discoverable" || bad "adapter still pairable/discoverable: $st"

# 1. a bonded but untrusted device must be refused while the window is closed
if bluetoothctl info "$PI_BT" 2>/dev/null | grep -q "Paired: yes"; then
    pi "bluetoothctl untrust $MY_BT >/dev/null 2>&1"
    r=$(try_session); echo "      $r"
    [[ "$r" == SESSION_FAILED* ]] && ok "untrusted bonded device refused (window closed)" || bad "untrusted device got a session"
    pi "grep -c 'refused service' ~/.local/state/arstro-remote/arstro-remote.log" >/dev/null
fi

# fresh start: forget each other
bluetoothctl remove "$PI_BT" >/dev/null 2>&1
pi "~/.local/bin/arstro-remote unpair $MY_BT >/dev/null 2>&1"

# 2. pairing must fail while the window is closed (device cached via a short open scan first)
pi '~/.local/bin/arstro-remote pair 120 >/dev/null'
discover || bad "host never discovered the Pi (radio)"
pi '~/.local/bin/arstro-remote pair 0 >/dev/null'
out=$(btctl "pair $PI_BT" 20 "info $PI_BT" 2)
why=$(echo "$out" | grep -oE "Failed to pair: [A-Za-z.]+" | head -1)
if echo "$out" | grep -q "Paired: yes"; then bad "paired while the window was closed"
elif [[ "$why" == *AlreadyExists* ]]; then bad "test setup: host still bonded ($why)"
else ok "pairing refused while window closed ($why)"; fi
bluetoothctl remove "$PI_BT" >/dev/null 2>&1

# 3. pairing works while the window is open, and the device becomes trusted
pi '~/.local/bin/arstro-remote pair 300 >/dev/null'
discover || bad "host never discovered the Pi (radio)"
paired=no
for i in 1 2 3 4 5; do   # the radio link is marginal: retry until a real bond exists
    out=$(btctl "pair $PI_BT" 25 "info $PI_BT" 2)
    if echo "$out" | grep -q "Paired: yes" && pi "bluetoothctl info $MY_BT" | grep -q "Bonded: yes"; then paired=yes; break; fi
    echo "      (pair attempt $i: $(echo "$out" | grep -oE "Failed to pair: [A-Za-z.]+" | head -1))"
    bluetoothctl remove "$PI_BT" >/dev/null 2>&1; pi "~/.local/bin/arstro-remote unpair $MY_BT >/dev/null 2>&1"
    discover >/dev/null
done
[ "$paired" = yes ] && ok "bonded while window open" || bad "could not bond with window open"
r=$(try_session); echo "      $r"
[[ "$r" == SESSION_OK* ]] && ok "new device gets a session" || bad "new device refused"
pi "~/.local/bin/arstro-remote status" | grep -q "($MY_BT)" && \
    ! pi "~/.local/bin/arstro-remote status" | grep -q "($MY_BT) NOT trusted" && ok "device trusted on the Pi" || bad "device not trusted"

# 4. a trusted device keeps working after the window closes
pi '~/.local/bin/arstro-remote pair 0 >/dev/null'
r=$(try_session); echo "      $r"
[[ "$r" == SESSION_OK* ]] && ok "trusted device allowed with window closed" || bad "trusted device refused"

# cleanup
bluetoothctl remove "$PI_BT" >/dev/null 2>&1
pi "~/.local/bin/arstro-remote unpair $MY_BT >/dev/null 2>&1; ~/.local/bin/arstro-remote pair 600 >/dev/null"
echo "== $pass passed, $fail failed"
[ "$fail" -eq 0 ]
