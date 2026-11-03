#!/usr/bin/env bash
# One folder with everything a new device manager needs (docs/design.md, RLY-20):
#
#   scripts/bundle.sh [--arm64-host user@host] [--no-amd64]
#
# Writes release/arstro-bundle-<date>/ (and .tar.gz):
#   device-manager/   the Docker kit for the server (relay/package.sh: image(s), compose, run.sh)
#   client/           what makes a computer a device: Arstro Remote + its installer (server/,
#                     install.sh) - and for phones the Arstro Remote Client APK
#   app/              Arstro Devices (the Android viewer) and Arstro Remote (the Pi remote) APKs
#   README.md, SHA256SUMS
# The APKs are taken from release/ (build them first: scripts/build_apk.sh --devices / --client /
# no option for the Pi remote); the versions are the ones in each app's pubspec.yaml. Oracle's
# Ampere VMs are arm64: --arm64-host builds that image on an arm64 machine with Docker (the VM itself).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }
case "${1:-}" in -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac

pubver() { sed -n 's/^version: *\([0-9.]*\).*/\1/p' "$ROOT/$1/pubspec.yaml"; }
RELAY_V=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/relay/arstro_relay/__init__.py")
SERVER_V=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/server/arstro_remote/__init__.py")
BACKUP_V=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/server/arstro_backup/__init__.py")
DEVICES_V=$(pubver devices_app)
CLIENT_V=$(pubver client_app)
REMOTE_V=$(pubver app)
APKS=("arstro-devices-v$DEVICES_V.apk:app" "arstro-remote-v$REMOTE_V.apk:app" "arstro-remote-client-v$CLIENT_V.apk:client")
for a in "${APKS[@]}"; do
    [ -f "$ROOT/release/${a%%:*}" ] || die "release/${a%%:*} is missing - build it with scripts/build_apk.sh"
done

NAME="arstro-bundle-$(date +%Y%m%d)"
OUT="$ROOT/release/$NAME"
rm -rf "${OUT:?}" "$OUT.tar.gz"
mkdir -p "$OUT/device-manager" "$OUT/client" "$OUT/app"

say "Device manager $RELAY_V (Docker kit)"
"$ROOT/relay/package.sh" "$@"
cp "$ROOT/release/arstro-devices-$RELAY_V.tar.gz" "$OUT/device-manager/"

say "Client: Arstro Remote $SERVER_V (backup terminal $BACKUP_V)"
C="arstro-remote-$SERVER_V"
mkdir -p "$OUT/client/$C"
# what is in git plus new files not ignored yet, without tests and caches
(cd "$ROOT" && git ls-files --cached --others --exclude-standard -- install.sh server |
    grep -v -e '^server/tests/' -e '__pycache__' | while read -r f; do [ -e "$f" ] && echo "$f"; done |
    tar -c -T - | tar -x -C "$OUT/client/$C")
cat > "$OUT/client/$C/JOIN.md" <<EOF
# Arstro Remote $SERVER_V - make this computer a device

    ./install.sh --relay https://<domain>/arstro/internal/devices --relay-token <join token>
    ./install.sh --backup        # optional: the backup terminal (port 8181), never touched by updates

Then open http://<this computer>:8080 (the password is asked during the install) and, on its
*Internet access* page, the access key that viewers need. Linux only (Debian / Ubuntu / Armbian).
EOF
tar -C "$OUT/client" -czf "$OUT/client/$C.tar.gz" "$C"
rm -rf "${OUT:?}/client/$C"

say "Apps"
for a in "${APKS[@]}"; do
    cp "$ROOT/release/${a%%:*}" "$OUT/${a##*:}/"
done

cat > "$OUT/README.md" <<EOF
# Arstro bundle $(date +%Y-%m-%d)

| Folder | What | Version |
|---|---|---|
| device-manager/ | the server everything connects to: \`tar xzf arstro-devices-$RELAY_V.tar.gz\`, then its README (\`./run.sh\`) | $RELAY_V |
| client/ | a computer joins with \`arstro-remote-$SERVER_V.tar.gz\` (JOIN.md inside); a phone with \`arstro-remote-client-v$CLIENT_V.apk\` | $SERVER_V / $CLIENT_V |
| app/ | \`arstro-devices-v$DEVICES_V.apk\`: every device in one Android app; \`arstro-remote-v$REMOTE_V.apk\`: the Pi remote | $DEVICES_V / $REMOTE_V |

1. Start the device manager on the server (\`./run.sh\` prints the join token).
2. Each computer: \`./install.sh --relay https://<domain>/arstro/internal/devices --relay-token <token>\`.
3. Open \`https://<domain>/arstro/internal/devices/\` in a browser, or add \`<domain>\` in Arstro Devices.
EOF
(cd "$OUT" && find . -type f ! -name SHA256SUMS ! -name README.md | sort | xargs sha256sum > SHA256SUMS)
tar -C "$ROOT/release" -czf "$OUT.tar.gz" "$NAME"
say "Done: $OUT ($(du -sh "$OUT" | cut -f1))"
