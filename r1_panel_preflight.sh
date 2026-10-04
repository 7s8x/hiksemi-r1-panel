#!/bin/sh
# Wait briefly for the integrated LCD and touchscreen on boot.
set -eu

test "$(cat /sys/class/dmi/id/board_name)" = HS-AFS-R1
attempt=0
while [ "$attempt" -lt 30 ]; do
    if [ -e /dev/fb0 ]; then
        for name in /sys/class/input/event*/device/name; do
            if [ -f "$name" ] && [ "$(cat "$name")" = 'CST0836:00 0911:5288' ]; then
                exit 0
            fi
        done
    fi
    attempt=$((attempt + 1))
    sleep 1
done
echo 'R1 LCD or touchscreen was not ready within 30 seconds' >&2
exit 1
