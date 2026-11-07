"""Read-only storage queries; scheduling and freshness belong to the collector."""
from __future__ import annotations
from r1_site_config import SITE
import time
import ipaddress
import json
from pathlib import Path
import re
import subprocess


CONFIG_PATH = Path("/var/lib/r1-panel/storage-usage.json")


def disk_generation(disks, by_id_root=Path("/dev/disk/by-id"), block_root=Path("/sys/block")):
    """A reused /dev/sdX name must not reuse the previous disk's cached capacity."""
    values = []
    for label, disk in disks:
        if disk is None:
            values.append((label, None, None))
            continue
        try:
            target = (by_id_root / disk).resolve(strict=True)
            sequence = (block_root / target.name / "diskseq").read_text().strip()
            if not sequence.isdecimal():
                return None
        except (OSError, RuntimeError):
            return None
        values.append((label, disk, sequence))
    return tuple(values)


def _run(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def _config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        host = str(ipaddress.ip_address(config["host"]))
        user = config["user"]
        vmid = config["vmid"]
        port = config.get("port", 22)
        identity = Path(config["identity_file"])
        known_hosts = Path(config["known_hosts_file"])
        if (not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or
                not isinstance(vmid, int) or not 100 <= vmid <= 999999 or
                not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 or
                not identity.is_absolute() or not known_hosts.is_absolute() or
                not identity.is_file() or not known_hosts.is_file()):
            return None
        return host, user, vmid, identity, known_hosts, port
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _guest_ssh_command(config):
    host, user, _, identity, known_hosts, port = config
    args = [
        "ssh", "-i", str(identity), "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "ConnectTimeout=3", "-o", "UpdateHostKeys=no",
        "-o", "ClearAllForwardings=yes", "-o", "PasswordAuthentication=no",
    ]
    if port != 22:
        # Keep matching the already pinned hostname when SSH moves ports.
        args += ["-p", str(port), "-o", f"HostKeyAlias={host}"]
    return args + ["-T", f"{user}@{host}", "r1-usage"]


def _host_sata(config):
    """Resolve VM SATA slots to current physical block devices."""
    output = _run(["qm", "config", str(config[2])], 4)
    result = {}
    for line in output.splitlines():
        match = re.match(r"^sata([0-9]+):\s+(/dev/[^,\s]+)", line)
        if not match:
            continue
        slot = int(match.group(1))
        try:
            target = Path(match.group(2)).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if target.name.startswith("sd"):
            result[target] = slot
    return result


def _map_guest_to_bays(disks, host_slots, guest, by_id_root=Path("/dev/disk/by-id")):
    mapped = [None] * len(disks)
    for index, (_, disk) in enumerate(disks):
        if not disk:
            continue
        path = by_id_root / disk
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        host_slot = host_slots.get(target)
        # Synology numbers VM100 SATA buses from 1 while PVE numbers from 0.
        if host_slot is not None:
            mapped[index] = guest.get(host_slot + 1)
    return mapped


def nvme_capacity():
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


def guest_capacity(config):
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


def bay_capacities(disks):
    """Query only the optional DSM source and map verified host SATA slots."""
    config = _config()
    if not config:
        return [None] * len(disks)
    return _map_guest_to_bays(disks, _host_sata(config), guest_capacity(config))
