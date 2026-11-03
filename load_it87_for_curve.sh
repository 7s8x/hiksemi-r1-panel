#!/bin/sh
# Load only the verified R1 IT8613E module built for this exact PVE kernel.
set -eu

if [ ! -d /sys/module/it87 ]; then
    : "${R1_IT87_MODULE:?configure an approved module path}"
    : "${R1_IT87_SHA256:?configure the approved module checksum}"
    script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    "$script_dir/.venv/bin/python" "$script_dir/r1_doctor.py" --check-driver
    /usr/sbin/modprobe hwmon_vid
    /usr/sbin/insmod "$R1_IT87_MODULE"
fi

count=0
for name in /sys/class/hwmon/hwmon*/name; do
    if [ "$(/usr/bin/cat "$name")" = it8613 ]; then
        count=$((count + 1))
    fi
done
test "$count" -eq 1
