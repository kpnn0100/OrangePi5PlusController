#!/usr/bin/env bash
# Arstro device manager @VERSION@ - run it from this folder (only Docker with the compose plugin needed).
#
#   ./run.sh [start]     load the image for this CPU, start the device manager + Caddy (HTTPS)
#   ./run.sh token       the join token the machines need (send it to the owner)
#   ./run.sh status      containers and the device list
#   ./run.sh logs        follow the logs
#   ./run.sh stop        stop (the data and the certificate stay in Docker volumes)
#
# DOMAIN (this server's name, already pointing at it) comes from the environment or .env.
set -euo pipefail
case "${1:-}" in -h|--help|help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac
cd "$(dirname "${BASH_SOURCE[0]}")"
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
command -v docker >/dev/null || die "Docker is not installed (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || die "the Docker compose plugin is missing (package docker-compose-plugin)"
IMAGE="arstro-devices:@VERSION@"
[ -f .env ] && set -a && . ./.env && set +a

load_image() {
    docker image inspect "$IMAGE" >/dev/null 2>&1 && return
    case "$(uname -m)" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) die "no image for this CPU ($(uname -m)): amd64 and arm64 only" ;;
    esac
    file="images/arstro-devices-@VERSION@-$arch.tar.gz"
    [ -f "$file" ] || die "$file is not in this kit (ask for the $arch image)"
    if [ -f SHA256SUMS ] && command -v sha256sum >/dev/null; then
        grep " $file\$" SHA256SUMS | sha256sum -c --quiet - || die "$file is damaged (checksum mismatch)"
    fi
    say "Loading $file"
    docker load -i "$file" >/dev/null
}

case "${1:-start}" in
    start)
        [ -n "${DOMAIN:-}" ] || die "set DOMAIN, e.g. DOMAIN=devices.example.com ./run.sh (or write it into .env)"
        load_image
        say "Starting the device manager for https://$DOMAIN"
        docker compose up -d
        echo
        echo "Device list : https://$DOMAIN${RELAY_BASE:-/arstro/internal/devices}/"
        echo "Join token  : ./run.sh token   (machines need it to appear in the list)"
        echo "The first start asks Let's Encrypt for a certificate: ports 80 and 443 must be open."
        ;;
    token)
        docker compose exec -T devices python -m arstro_relay admin token
        ;;
    status)
        docker compose ps
        docker compose exec -T devices python -m arstro_relay admin list || true
        ;;
    logs)
        docker compose logs -f --tail 100
        ;;
    stop)
        docker compose down
        ;;
    *)
        die "unknown command $1 (start, token, status, logs, stop)"
        ;;
esac
