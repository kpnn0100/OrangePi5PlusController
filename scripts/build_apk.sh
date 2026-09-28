#!/usr/bin/env bash
# Build the signed release APK of the Android app into release/ (SET-03).
#
#   scripts/build_apk.sh                      check toolchain, pub get, analyze, test, build
#   scripts/build_apk.sh --no-test            skip analyze + tests
#   scripts/build_apk.sh --install [SERIAL]   also adb-install it (SERIAL or $ANDROID_SERIAL)
#   scripts/build_apk.sh --debug-sign         never create a release key (use the debug key)
#
# Needs Flutter >= 3.38 (Dart 3.10), an Android SDK with platform 36 and JDK 17.
# Release signing uses app/android/key.properties + the keystore it names (both gitignored).
# If they are missing a new key is created there (keep a backup: an APK signed with
# another key cannot update an installed one - uninstall the old app first).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/app"
KEYPROPS="$APP/android/key.properties"
TEST=1 INSTALL=0 SERIAL="${ANDROID_SERIAL:-}" DEBUG_SIGN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-test) TEST=0; shift ;;
        --install) INSTALL=1; shift; if [ $# -gt 0 ] && [[ "$1" != -* ]]; then SERIAL="$1"; shift; fi ;;
        --debug-sign) DEBUG_SIGN=1; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
done
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ -f "$APP/pubspec.yaml" ] || die "app not found at $APP"

step "Toolchain"
command -v flutter >/dev/null || die "flutter not found - install Flutter 3.38+ (https://docs.flutter.dev/get-started/install)"
fv=$(flutter --version 2>/dev/null | sed -n 's/^Flutter \([0-9.]*\).*/\1/p' | head -1)
echo "Flutter $fv"
IFS=. read -r ma mi _ <<<"$fv"
if [ "${ma:-0}" -lt 3 ] || { [ "$ma" -eq 3 ] && [ "${mi:-0}" -lt 38 ]; }; then
    warn "Flutter $fv is older than 3.38 (the project is built with 3.38.5) - expect errors"
fi
doctor=$(flutter doctor 2>/dev/null | grep -A3 "Android toolchain" || true)
echo "$doctor" | head -4
if echo "$doctor" | grep -q "cmdline-tools component is missing"; then
    warn "Android cmdline-tools are missing. The build usually still works; to fix it install"
    warn "'Android SDK Command-line Tools' (Android Studio > SDK Manager > SDK Tools) and run"
    warn "'flutter doctor --android-licenses'."
fi

step "Signing"
keystore_of() { sed -n 's/^storeFile=//p' "$KEYPROPS" | head -1; }
if [ -f "$KEYPROPS" ] && [ -n "$(keystore_of)" ] && [ -f "$APP/android/app/$(keystore_of)" ]; then
    echo "release key: app/android/app/$(keystore_of)"
elif [ "$DEBUG_SIGN" = 1 ]; then
    warn "no release key -> signed with the DEBUG key (--debug-sign)"
else
    command -v keytool >/dev/null || die "keytool not found (install JDK 17), or use --debug-sign"
    store="arstro-release.jks"
    [ -e "$APP/android/app/$store" ] && die "app/android/app/$store exists but key.properties is missing or wrong - fix it or move the keystore away"
    pass=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 28)
    keytool -genkeypair -noprompt -keystore "$APP/android/app/$store" -storetype PKCS12 -keyalg RSA -keysize 4096 \
        -validity 10000 -alias arstro -dname "CN=Arstro Remote" -storepass "$pass" -keypass "$pass" >/dev/null 2>&1 \
        || die "keytool failed"
    umask 077
    printf 'storePassword=%s\nkeyPassword=%s\nkeyAlias=arstro\nstoreFile=%s\n' "$pass" "$pass" "$store" > "$KEYPROPS"
    chmod 600 "$KEYPROPS" "$APP/android/app/$store"
    warn "created a NEW release key: app/android/app/$store (+ app/android/key.properties)."
    warn "Back both up. Phones with an APK signed by another key must uninstall it first."
fi

cd "$APP"
step "flutter pub get"
flutter pub get >/dev/null && echo ok

if [ "$TEST" = 1 ]; then
    step "flutter analyze"
    flutter analyze
    step "flutter test"
    log=$(mktemp)
    if flutter test --reporter expanded >"$log" 2>&1; then
        tail -1 "$log"
        rm -f "$log"
    else
        grep -A12 "\[E\]" "$log" | head -60
        tail -1 "$log"
        rm -f "$log"
        die "tests failed (fix them, or build anyway with --no-test)"
    fi
fi

step "flutter build apk --release"
rm -f build/app/outputs/flutter-apk/app-release.apk
flutter build apk --release 2>&1 | grep -E "Built|rror|FAILED" || true
[ -f build/app/outputs/flutter-apk/app-release.apk ] || die "build failed (run: cd app && flutter build apk --release -v)"
ver=$(sed -n 's/^version: *\([0-9.]*\).*/\1/p' pubspec.yaml)
mkdir -p "$ROOT/release"
out="$ROOT/release/arstro-remote-v$ver.apk"
cp build/app/outputs/flutter-apk/app-release.apk "$out"
(cd "$ROOT/release" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
echo "APK: $out"

if [ "$INSTALL" = 1 ]; then
    step "adb install"
    command -v adb >/dev/null || die "adb not found (Android SDK platform-tools)"
    if [ -z "$SERIAL" ] && [ "$(adb devices | grep -c 'device$')" -gt 1 ]; then
        die "several devices attached - pass --install SERIAL (see 'adb devices')"
    fi
    adb ${SERIAL:+-s "$SERIAL"} install -r "$out" | tail -1
fi
