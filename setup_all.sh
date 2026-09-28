#!/usr/bin/env bash
# Set up the Pi, build and install the app in one go (see scripts/setup_all.sh --help).
exec "$(dirname "${BASH_SOURCE[0]}")/scripts/setup_all.sh" "$@"
