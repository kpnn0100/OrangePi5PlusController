#!/bin/bash
# Arstro Remote server installer - see server/install.sh for the options:
#
#   ./install.sh                  slot A, port 8080 (asks for the web password; Enter = admin)
#   ./install.sh --slot b         slot B, port 8081, next to a running A
#   ./install.sh --modules monitor,system,terminal,io,files   only what this machine needs
#   ./install.sh --backup         the backup terminal on port 8181 (slot installs never touch it)
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/server/install.sh" "$@"
