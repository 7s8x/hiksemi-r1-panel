#!/bin/sh
# Leave a usable local console if the display process exits unexpectedly.
set -u

printf '0\n' > /sys/class/graphics/fb0/blank 2>/dev/null || true
printf '0\n' > /sys/class/backlight/intel_backlight/bl_power 2>/dev/null || true
brightness=$(cat /sys/class/backlight/intel_backlight/brightness 2>/dev/null) || brightness=0
case "$brightness" in
    ''|*[!0-9]*|0) brightness=36960 ;;
esac
printf '%s\n' "$brightness" > /sys/class/backlight/intel_backlight/brightness 2>/dev/null || true
printf '\033[?25h' > /dev/tty2 2>/dev/null || true
/usr/bin/chvt 1 2>/dev/null || true
