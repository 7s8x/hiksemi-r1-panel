"""Read-only, best-effort usage for the R1 NVMe and VM100 passthrough bays.

The host cannot infer a guest filesystem's free space from a raw disk. SATA
percentages are accepted only from a restricted, optional DSM SSH collector.
Missing or ambiguous mappings remain unknown rather than showing a wrong bay.
"""

import ipaddress
import json
from pathlib import Path
import re
import subprocess
import time


CONFIG_PATH = Path("/var/lib/r1-panel/storage-usage.json")
REFRESH_SECONDS = 60
_cached_at = 0.0
_cached_disks = None
_cached_usage = None


def _run(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def _nvme_usage():
    """Percent of the host's local and thin PVE stores combined."""
    output = _run(["pvesm", "status"], 4)
    total = used = 0
    seen = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] not in ("local", "local-lvm"):
            continue
        try:
            capacity, occupied = int(fields[3]), int(fields[4])
        except ValueError:
            continue
        if capacity <= 0 or occupied < 0 or occupied > capacity:
            continue
        seen.add(fields[0])
        total += capacity
        used += occupied
    if seen != {"local", "local-lvm"} or total <= 0:
        # The Web service cannot open /dev/mapper/control. PVE's own recent
        # storage history remains readable without widening its device access.
        total = used = 0
        for name in ("local", "local-lvm"):
            output = _run(["pvesh", "get", f"/nodes/pve/storage/{name}/rrddata",
                           "--timeframe", "hour", "--cf", "AVERAGE",
                           "--output-format", "json"], 4)
            try:
                samples = json.loads(output)
                sample = next(row for row in reversed(samples)
                              if row.get("total") and row.get("used") is not None)
                capacity, occupied = int(sample["total"]), int(sample["used"])
                age = time.time() - int(sample["time"])
                if not 0 <= age <= 300 or not 0 <= occupied <= capacity:
                    return None
            except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
                return None
            total += capacity
            used += occupied
        if total <= 0:
            return None
    return min(100, round(100 * used / total))


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


def _guest_usage(config):
    output = _run(_guest_ssh_command(config), 6)
    result = {}
    for line in output.splitlines():
        match = re.fullmatch(r"SATA\s+([0-9]+)\s+([0-9]{1,3})", line.strip())
        if not match:
            continue
        slot, percent = map(int, match.groups())
        if 1 <= slot <= 32 and 0 <= percent <= 100:
            result[slot] = percent
    return result


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


def storage_usage(disks):
    """Return NVMe and four bay percentages, or None where not verified."""
    global _cached_at, _cached_disks, _cached_usage
    disks = tuple(disks)
    now = time.monotonic()
    if disks == _cached_disks and _cached_usage is not None and now - _cached_at < REFRESH_SECONDS:
        return list(_cached_usage)
    usage = [_nvme_usage()] + [None] * len(disks)
    config = _config()
    if config:
        guest = _guest_usage(config)
        host_slots = _host_sata(config)
        usage[1:] = _map_guest_to_bays(disks, host_slots, guest)
    _cached_at, _cached_disks, _cached_usage = now, disks, tuple(usage)
    return usage
