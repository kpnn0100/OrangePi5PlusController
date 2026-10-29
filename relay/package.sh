#!/usr/bin/env bash
# Package the device manager for someone who only runs Docker (e.g. a friend's Oracle VM) - RLY-20.
#
#   relay/package.sh [--arm64-host user@host] [--no-amd64]
#
# Writes release/arstro-devices-<version>/ and release/arstro-devices-<version>.tar.gz (send that):
#   images/arstro-devices-<version>-<arch>.tar.gz   the image (docker save | gzip), one per architecture
#   docker-compose.yml   the stack without a build step: the image + Caddy (automatic HTTPS)
#   Caddyfile, .env.example, run.sh (load the right image, start, token, logs), README.md
#
# The amd64 image is built here (when this machine is x86_64). Oracle's free Ampere VMs are arm64:
# --arm64-host builds that image natively on an arm64 machine with Docker over SSH (e.g. the
# Orange Pi), from the same files, and streams it back. release/ is not committed (.gitignore).
# covers: RLY-20
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARM_HOST="" AMD64=1
while [ $# -gt 0 ]; do
    case "$1" in
        --arm64-host) ARM_HOST="$2"; shift 2 ;;
        --no-amd64) AMD64=0; shift ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }
command -v docker >/dev/null || die "docker is not installed"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/relay/arstro_relay/__init__.py")
[ -n "$VERSION" ] || die "no __version__ in relay/arstro_relay/__init__.py"
IMAGE="arstro-devices:$VERSION"
NAME="arstro-devices-$VERSION"
OUT="$ROOT/release/$NAME"
# what the Dockerfile needs from the repository (build context = repo root)
CONTEXT=(.dockerignore relay server/arstro_remote/relay/client.py server/arstro_remote/relay/e2e.py
         server/arstro_remote/web/ws.py server/arstro_remote/relay/pipe.py)

rm -rf "$OUT" "$OUT.tar.gz"
mkdir -p "$OUT/images"
built=0
if [ "$AMD64" = 1 ]; then
    [ "$(uname -m)" = x86_64 ] || die "this machine is $(uname -m): use --no-amd64 or build on an x86_64 machine"
    say "Building $IMAGE for amd64 here"
    docker build -q -f "$ROOT/relay/Dockerfile" -t "$IMAGE" "$ROOT" >/dev/null
    docker save "$IMAGE" | gzip -6 > "$OUT/images/$NAME-amd64.tar.gz"
    built=$((built + 1))
fi
if [ -n "$ARM_HOST" ]; then
    say "Building $IMAGE for arm64 on $ARM_HOST"
    [ "$(ssh -o BatchMode=yes "$ARM_HOST" uname -m)" = aarch64 ] || die "$ARM_HOST is not an arm64 (aarch64) machine"
    (cd "$ROOT" && tar -c --exclude=__pycache__ --exclude=node_modules "${CONTEXT[@]}") |
        ssh -o BatchMode=yes "$ARM_HOST" "set -e; d=\$(mktemp -d); trap 'rm -rf \$d' EXIT; tar -x -C \$d;
            docker build -q -f \$d/relay/Dockerfile -t $IMAGE \$d >/dev/null; docker save $IMAGE" |
        gzip -6 > "$OUT/images/$NAME-arm64.tar.gz"
    built=$((built + 1))
fi
[ "$built" -gt 0 ] || die "nothing to build (--no-amd64 without --arm64-host)"

say "Writing the kit"
# the compose file without its build step, pinned to this version (one definition: relay/docker-compose.yml)
python3 - "$ROOT/relay/docker-compose.yml" "$OUT/docker-compose.yml" "$IMAGE" <<'EOF'
import re, sys
src, dst, image = sys.argv[1:]
text = open(src).read()
text = re.sub(r"\n    build:\n(      .*\n)+", "\n", text)
text = text.replace("    image: arstro-devices\n", "    image: %s\n" % image)
head = ("# The Arstro device manager: start it with ./run.sh (see README.md). Made by relay/package.sh\n"
        "# from relay/docker-compose.yml - change that one, not this copy.\n")
text = head + "\n".join(l for l in text.splitlines() if not l.startswith("#")) + "\n"
assert "build:" not in text and image in text, "could not turn the compose file into an image-only one"
open(dst, "w").write(text)
EOF
cp "$ROOT/relay/Caddyfile" "$OUT/Caddyfile"
cp "$ROOT/relay/kit/run.sh" "$ROOT/relay/kit/README.md" "$ROOT/relay/kit/.env.example" "$OUT/"
chmod +x "$OUT/run.sh"
sed -i "s/@VERSION@/$VERSION/g" "$OUT/README.md" "$OUT/run.sh"
(cd "$OUT" && sha256sum images/*.tar.gz > SHA256SUMS)
tar -C "$ROOT/release" -czf "$OUT.tar.gz" "$NAME"
say "Done: $OUT.tar.gz ($(du -h "$OUT.tar.gz" | cut -f1))"
ls -l "$OUT/images" | tail -n +2
