#!/bin/sh
# Load only the verified R1 IT8613E module built for this exact PVE kernel.
set -eu

module=/root/r1-it87-test-bd7a566c/it87.ko
expected_sha=6e31593586ca9f66a50bdcd021a3246aa30094069c602ac94c054d99da17d739

if [ ! -d /sys/module/it87 ]; then
    test -f "$module"
    test "$(/usr/bin/sha256sum "$module" | /usr/bin/cut -d ' ' -f 1)" = "$expected_sha"
    kernel=$(/usr/bin/uname -r)
    case "$(/usr/sbin/modinfo -F vermagic "$module")" in
        "$kernel "*) ;;
        *) echo "it87 module does not match running kernel" >&2; exit 1 ;;
    esac
    /usr/sbin/modprobe hwmon_vid
    /usr/sbin/insmod "$module"
fi

count=0
for name in /sys/class/hwmon/hwmon*/name; do
    if [ "$(/usr/bin/cat "$name")" = it8613 ]; then
        count=$((count + 1))
    fi
done
test "$count" -eq 1
