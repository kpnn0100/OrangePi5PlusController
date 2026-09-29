#!/bin/bash
# Install the Debian/Ubuntu packages the chosen modules need (run as root; install.sh
# calls it through sudo). Only missing packages are installed; optional ones that apt
# cannot install are reported, not fatal.
#
#   install_deps.sh [--check] MODULE...      (modules: see server/arstro_remote/modules.py)
set -u
CHECK=0
[ "${1:-}" = "--check" ] && { CHECK=1; shift; }
mods=" $* "

need=(python3 python3-psutil python3-gi python3-dbus gir1.2-glib-2.0 bluez)
optional=()
case "$mods" in *" connection "*) need+=(network-manager) ;; esac
case "$mods" in *" screen "*)
    need+=(libxtst6)
    optional+=(gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
               gstreamer1.0-plugins-ugly) ;;
esac
case "$mods" in *" camera "*)
    optional+=(gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
               gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly ffmpeg v4l-utils) ;;
esac

installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed"; }
missing=() missing_opt=()
for p in "${need[@]}"; do installed "$p" || missing+=("$p"); done
for p in $(printf '%s\n' "${optional[@]}" | sort -u); do installed "$p" || missing_opt+=("$p"); done

if [ "$CHECK" = 1 ]; then
    echo "${missing[*]}${missing[*]:+ }${missing_opt[*]}"
    exit 0
fi
if ! command -v apt-get >/dev/null; then
    [ ${#missing[@]} -eq 0 ] && [ ${#missing_opt[@]} -eq 0 ] && exit 0
    echo "No apt-get here - install these yourself: ${missing[*]} ${missing_opt[*]}" >&2
    [ ${#missing[@]} -eq 0 ]
    exit $?
fi
[ "$(id -u)" = 0 ] || { echo "install_deps.sh must run as root" >&2; exit 1; }
if [ ${#missing[@]} -gt 0 ] || [ ${#missing_opt[@]} -gt 0 ]; then
    apt-get update -qq || true
fi
rc=0
if [ ${#missing[@]} -gt 0 ]; then
    echo "  installing: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" || rc=1
fi
for p in "${missing_opt[@]}"; do            # one by one: a held or broken package must not block the rest
    echo "  installing (optional): $p"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 ||
        echo "  WARNING: could not install $p - the feature that needs it stays off" >&2
done
exit $rc
