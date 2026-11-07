"""Read-only capacity figures for the Web disk cards.

The shared display collector schedules the private query helpers independently.
DSM reports per-volume KiB through its existing forced SSH command; PVE RRD supplies the
host stores when the Web service cannot access the LVM control device.
"""

from __future__ import annotations

from r1_site_config import SITE
import json
import re
import time

from r1_storage_usage import _config, _guest_ssh_command, _host_sata, _map_guest_to_bays, _run


def _nvme_capacity():
    total = used = available = 0
    for name in SITE.stores:
        output = _run(["pvesh", "get", f"/nodes/{SITE.node}/storage/{name}/status",
                       "--output-format", "json"], 4)
        try:
            record = json.loads(output)
            size, occupied, free = (int(record[key]) for key in ("total", "used", "avail"))
            if size <= 0 or not 0 <= occupied <= size or not 0 <= free <= size:
                raise ValueError("invalid storage status")
        except (ValueError, TypeError, KeyError):
            output = _run(["pvesh", "get", f"/nodes/{SITE.node}/storage/{name}/rrddata",
                           "--timeframe", "hour", "--cf", "AVERAGE",
                           "--output-format", "json"], 4)
            try:
                samples = json.loads(output)
                sample = next(row for row in reversed(samples)
                              if row.get("total") and row.get("used") is not None)
                size, occupied = int(sample["total"]), int(sample["used"])
                age = time.time() - int(sample["time"])
                if size <= 0 or not 0 <= age <= 300 or not 0 <= occupied <= size:
                    print(f"web_nvme_rrd_invalid={name}:age={age}", flush=True)
                    return None
            except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as error:
                print(f"web_nvme_rrd_error={name}:{type(error).__name__}:bytes={len(output)}", flush=True)
                return None
            free = size - occupied
        total += size
        used += occupied
        available += free
    return (used, available, round(100 * used / total)) if total > 0 else None


def _guest_capacity(config):
    output = _run(_guest_ssh_command(config), 6)
    result = {}
    for line in output.splitlines():
        match = re.fullmatch(r"CAPACITY\s+([0-9]+)\s+([0-9]{1,3})\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)", line.strip())
        if not match:
            continue
        slot, percent, total_kib, used_kib, free_kib = map(int, match.groups())
        if (1 <= slot <= 32 and 0 <= percent <= 100 and total_kib > 0
                and 0 <= used_kib <= total_kib and 0 <= free_kib <= total_kib
                and used_kib + free_kib <= total_kib):
            result[slot] = (used_kib * 1024, free_kib * 1024, percent)
    return result
