#!/bin/bash
# Give a user access to the IO devices (IO-01): GPIO character devices, I2C, SPI, serial
# ports, PWM and LEDs - through groups (gpio, i2c, spi, dialout) and udev rules. The
# desktop user also gets them at once through logind's uaccess ACLs; the groups count
# from the next login. Run as root (install.sh calls it through sudo).
#
#   setup_io_perms.sh USER [--headless]     --headless: also let USER manage NetworkManager
#                                            from a systemd user service (polkit) + linger
set -eu
USER_NAME="${1:?usage: setup_io_perms.sh USER [--headless]}"
HEADLESS=0
[ "${2:-}" = "--headless" ] && HEADLESS=1
[ "$(id -u)" = 0 ] || { echo "setup_io_perms.sh must run as root" >&2; exit 1; }
id "$USER_NAME" >/dev/null

RULES=/etc/udev/rules.d/70-arstro-io.rules
tmp=$(mktemp)
cat >"$tmp" <<'EOF'
# arstro-io-rules v1 - Arstro Remote (install.sh): IO access for the groups gpio / i2c / spi and, through
# uaccess, for the user logged in at the machine. Sorted before 73-seat-late.rules.
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="i2c-dev", GROUP="i2c", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="spidev", GROUP="spi", MODE="0660", TAG+="uaccess"
# sysfs PWM: the chip's files and every exported channel (the kernel sends "change")
SUBSYSTEM=="pwm", ACTION=="add|change", RUN+="/bin/sh -c 'chgrp -R gpio /sys%p && chmod -R g+w /sys%p'"
SUBSYSTEM=="leds", ACTION=="add", RUN+="/bin/sh -c 'chgrp gpio /sys%p/brightness /sys%p/trigger && chmod g+w /sys%p/brightness /sys%p/trigger'"
EOF

for g in gpio i2c spi dialout; do
    getent group "$g" >/dev/null || groupadd --system "$g"
done
missing=""
for g in gpio i2c spi dialout; do
    id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$g" || missing="$missing,$g"
done
if [ -n "$missing" ]; then
    usermod -aG "${missing#,}" "$USER_NAME"
    echo "  added $USER_NAME to ${missing#,} (counts from the next login)"
fi

if ! cmp -s "$tmp" "$RULES" 2>/dev/null; then
    install -m 644 "$tmp" "$RULES"
    echo "  wrote $RULES"
fi
rm -f "$tmp"
udevadm control --reload-rules
for s in gpio i2c-dev spidev pwm leds; do
    udevadm trigger --subsystem-match="$s" --action=add 2>/dev/null || true
done
udevadm settle -t 5 2>/dev/null || true

if [ "$HEADLESS" = 1 ]; then
    loginctl enable-linger "$USER_NAME" 2>/dev/null || true
    if [ -d /etc/polkit-1/rules.d ]; then
        cat >/etc/polkit-1/rules.d/50-arstro-remote.rules <<EOF
// Arstro Remote (install.sh --boot systemd): $USER_NAME may manage NetworkManager
// from its systemd user service, which is not part of an active login session.
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 && subject.user == "$USER_NAME")
        return polkit.Result.YES;
});
EOF
        echo "  wrote /etc/polkit-1/rules.d/50-arstro-remote.rules"
    fi
fi
exit 0
