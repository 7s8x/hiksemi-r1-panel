"""Read-only, best-effort usage for the R1 NVMe and VM100 passthrough bays.

The host cannot infer a guest filesystem's free space from a raw disk. SATA
percentages are accepted only from a restricted, optional DSM SSH collector.
Missing or ambiguous mappings remain unknown rather than showing a wrong bay.
"""

import ipaddress
from r1_site_config import SITE
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
