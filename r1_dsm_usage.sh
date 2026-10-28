#!/bin/sh
# Forced-command target for the R1 read-only DSM usage key.
# Emits one SATA slot and used percentage per single-disk data volume.
set -eu

data_sata() {
    for child in "$1"/slaves/*; do
        [ -e "$child" ] || continue
        name=${child##*/}
        case "$name" in
            sata[0-9]*p[0-9]*) printf '%s\n' "${name%%p*}" ;;
            *) data_sata "/sys/block/$name" ;;
        esac
    done
}

for volume in /volume[0-9]*; do
    [ -d "$volume" ] || continue
    line=$(df -Pk "$volume" 2>/dev/null | tail -n 1) || continue
    set -- $line
    [ "$#" -ge 6 ] || continue
    [ "$6" = "$volume" ] || continue
    device=$1
    percent=${5%%%}
    case "$device:$percent" in
        /dev/mapper/cachedev_*:[0-9]* ) ;;
        *) continue ;;
    esac
    case "$percent" in *[!0-9]*|'') continue ;; esac
    [ "$percent" -le 100 ] || continue
    cache=${device##*/}
    for block in /sys/block/dm-*; do
        [ -f "$block/dm/name" ] || continue
        [ "$(cat "$block/dm/name")" = "$cache" ] || continue
        slots=$(data_sata "$block")
        [ "$(printf '%s\n' "$slots" | wc -l)" -eq 1 ] || continue
        case "$slots" in
            sata[0-9]*) printf 'SATA %s %s\n' "${slots#sata}" "$percent" ;;
        esac
    done
done
