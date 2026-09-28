#!/usr/bin/env bash
# Build the signed release APK into release/ (see scripts/build_apk.sh --help).
exec "$(dirname "${BASH_SOURCE[0]}")/scripts/build_apk.sh" "$@"
