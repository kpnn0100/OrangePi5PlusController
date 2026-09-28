#!/usr/bin/env bash
# Set up everything in one go (SET-01): check this machine's tools, set up the Pi over
# SSH, build the signed APK and (optionally) install it on a phone.
#
#   scripts/setup_all.sh [options] [user@]PI_HOST
#
# Settings come from the arguments or from local.env in the repo root (gitignored, see
# local.env.example): ARSTRO_PI, ARSTRO_PI_PORT, ANDROID_SERIAL, ARSTRO_WEB_PASS.
# The Pi's login password is asked once (or ARSTRO_PASS); after that an SSH key is used.
#
# Options
#   --install [SERIAL]   install the APK with adb (SERIAL or ANDROID_SERIAL; see 'adb devices')
#   --web-password       set the web / remote-CLI password on the Pi (asked)
#   --autologin          turn on desktop auto-login on the Pi (the service starts with it)
#   --reboot             reboot the Pi if the service is not running yet
#   --pi-tests           run the server test suites on the Pi at the end
#   --no-pi              skip the Pi         --no-apk   skip the APK
#   --no-test            skip Flutter analyze + tests
#   -h, --help
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/local.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/local.env"
    set +a
fi

PI="${ARSTRO_PI:-}" DO_PI=1 DO_APK=1 INSTALL=0 SERIAL="${ANDROID_SERIAL:-}"
PI_OPTS=() APK_OPTS=()
[ -n "${ARSTRO_PI_PORT:-}" ] && PI_OPTS+=(--port "$ARSTRO_PI_PORT")
while [ $# -gt 0 ]; do
    case "$1" in
        --install)  # optional SERIAL: a word without '@' or '.' (those are Pi addresses)
            INSTALL=1; shift
            if [ $# -gt 0 ] && [[ "$1" != -* && "$1" != *@* && "$1" != *.* ]]; then SERIAL="$1"; shift; fi ;;
        --web-password) PI_OPTS+=(--web-password); shift ;;
        --autologin) PI_OPTS+=(--autologin); shift ;;
        --reboot) PI_OPTS+=(--reboot); shift ;;
        --pi-tests) PI_OPTS+=(--test); shift ;;
        --no-pi) DO_PI=0; shift ;;
        --no-apk) DO_APK=0; shift ;;
        --no-test) APK_OPTS+=(--no-test); shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
        *) PI="$1"; shift ;;
    esac
done

step() { printf '\n\033[1;36m######## %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

# ------------------------------------------------------------------ tools
step "This machine"
missing=0
need() {  # need CMD "why" [optional]
    if command -v "$1" >/dev/null; then ok "$1"
    elif [ "${3:-}" = optional ]; then warn "$1 not found ($2)"
    else bad "$1 not found ($2)"; missing=1; fi
}
[ "$DO_PI" = 1 ] && { need ssh "to set up the Pi"; need tar "to copy the server"; }
need python3 "the CLI and the tests" optional
if [ "$DO_APK" = 1 ]; then
    need flutter "to build the app - https://docs.flutter.dev/get-started/install"
    need java "JDK 17 for the Android build"
    need keytool "to create the signing key (JDK)" optional
fi
[ "$INSTALL" = 1 ] && need adb "to install the APK (Android platform-tools)"
need node "browser tests of the web UI (server/tests/web)" optional
[ "$missing" = 0 ] || { echo; echo "Install the missing tools above, then run this again." >&2; exit 1; }

# ------------------------------------------------------------------ Pi
if [ "$DO_PI" = 1 ]; then
    if [ -z "$PI" ]; then
        if { exec 3</dev/tty; } 2>/dev/null; then
            read -rp "Pi address ([user@]host, e.g. orangepi@orangepi5plus.local): " PI <&3; exec 3<&-
        fi
    fi
    [ -n "$PI" ] || { echo "give the Pi as [user@]HOST (or ARSTRO_PI in local.env), or use --no-pi" >&2; exit 2; }
    step "Pi $PI"
    "$ROOT/scripts/setup_pi.sh" "${PI_OPTS[@]}" "$PI"
fi

# ------------------------------------------------------------------ app
if [ "$DO_APK" = 1 ]; then
    step "Android app"
    [ "$INSTALL" = 1 ] && APK_OPTS+=(--install ${SERIAL:+"$SERIAL"})
    "$ROOT/scripts/build_apk.sh" "${APK_OPTS[@]}"
fi

step "All set"
host="${PI#*@}"; host="${host:-<pi>}"
echo "  Web UI : http://$host:8080/   (password: ssh ${PI:-<pi>} '~/.local/bin/arstro-remote web --show')"
echo "  CLI    : ssh ${PI:-<pi>} '~/.local/bin/arstro-remote status'   or   server/: python3 -m arstro_remote --url http://<pi>:8080 status"
echo "  Phone  : open Arstro Remote, Scan, pick \"Arstro-<hostname>\" (pairing is open 10 min after boot or 'arstro-remote pair')"
