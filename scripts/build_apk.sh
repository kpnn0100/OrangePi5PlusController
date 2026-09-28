#!/usr/bin/env bash
# Build the Android app on this machine and put the APK in release/.
#
#   ./build_app.sh                 check toolchain, pub get, analyze, test, release APK
#   ./build_app.sh --no-test       skip analyze + tests
#   ./build_app.sh --install [SERIAL]   also adb-install the APK
#
# Needs: Flutter >= 3.38 (Dart 3.10), Android SDK with platform 36, JDK 17
# ("flutter doctor" must show the Android toolchain). Release signing uses
# android_app/android/key.properties + app/arstro-release.jks when present
# (they are in the pack from pack.sh); otherwise the APK is signed with the
# debug key and cannot update an APK signed with the release key.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/android_app"
TEST=1 INSTALL=0 SERIAL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-test) TEST=0; shift ;;
        --install) INSTALL=1; shift; if [ $# -gt 0 ] && [[ "$1" != -* ]]; then SERIAL="$1"; shift; fi ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }

step "Toolchain"
command -v flutter >/dev/null || { echo "flutter not found - install Flutter 3.38+ (https://docs.flutter.dev/get-started/install)"; exit 1; }
fv=$(flutter --version 2>/dev/null | sed -n 's/^Flutter \([0-9.]*\).*/\1/p' | head -1)
echo "Flutter $fv"
IFS=. read -r ma mi _ <<<"$fv"
if [ "${ma:-0}" -lt 3 ] || { [ "$ma" -eq 3 ] && [ "${mi:-0}" -lt 38 ]; }; then
    warn "Flutter $fv is older than 3.38 (the project was built with 3.38.5) - expect errors"
fi
flutter doctor 2>/dev/null | grep -E "Android toolchain|✗" | head -5 || true
if [ -f "$APP/android/key.properties" ] && [ -f "$APP/android/app/arstro-release.jks" ]; then
    echo "release signing key: present"
else
    warn "release signing key missing -> the APK will be signed with the DEBUG key"
fi

cd "$APP"
step "flutter pub get"
flutter pub get >/dev/null 2>&1 && echo ok

if [ "$TEST" = 1 ]; then
    step "flutter analyze"
    flutter analyze
    step "flutter test"
    flutter test --reporter expanded 2>&1 | grep -E "Some tests failed|All tests passed|\[E\]" | tail -5
fi

step "flutter build apk --release"
flutter build apk --release 2>&1 | grep -E "Built|error|Error|FAILED" || true
ver=$(sed -n 's/^version: *\([0-9.]*\).*/\1/p' pubspec.yaml)
mkdir -p "$ROOT/release"
out="$ROOT/release/arstro-remote-v$ver.apk"
cp build/app/outputs/flutter-apk/app-release.apk "$out"
(cd "$ROOT/release" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
echo "APK: $out"

if [ "$INSTALL" = 1 ]; then
    step "adb install"
    adb ${SERIAL:+-s "$SERIAL"} install -r "$out" | tail -1
fi
