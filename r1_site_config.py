"""Validated, non-secret host configuration, loaded once at process startup."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class SiteConfig:
    node: str = "pve"
    vmids: tuple[int, ...] = (100, 111)
    stores: tuple[str, ...] = ("local", "local-lvm")
    controller: str = "0000:04:00.0"
    bay_ports: tuple[tuple[int, int], ...] = ((1, 4), (2, 3), (3, 2), (4, 1))
    occupied_bays: tuple[int, ...] = (1, 2, 3, 4)


def load_site(path=None):
    configured = path is not None or "R1_SITE_CONFIG" in os.environ
    source = Path(path or os.environ.get("R1_SITE_CONFIG", "/var/lib/r1-panel/site.json"))
    try:
        record = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not configured:
            return SiteConfig()
        raise ValueError("explicit site configuration is missing") from None
    except (OSError, ValueError):
        raise ValueError("site configuration cannot be read") from None
    if not isinstance(record, dict) or set(record) - {"node", "vmids", "stores", "controller", "bay_ports", "occupied_bays"}:
        raise ValueError("invalid site configuration fields")
    defaults = SiteConfig()
    node = record.get("node", defaults.node)
    vmids = record.get("vmids", defaults.vmids)
    stores = record.get("stores", defaults.stores)
    controller = record.get("controller", defaults.controller)
    ports = record.get("bay_ports", defaults.bay_ports)
    occupied = record.get("occupied_bays", defaults.occupied_bays)
    def identifier(value):
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value)
    if not identifier(node):
        raise ValueError("invalid PVE node")
    if (not isinstance(vmids, (list, tuple)) or not 0 <= len(vmids) <= 2 or
            any(type(v) is not int or not 100 <= v <= 999999 for v in vmids) or len(set(vmids)) != len(vmids)):
        raise ValueError("configure at most two distinct VM IDs")
    if (not isinstance(stores, (list, tuple)) or not 1 <= len(stores) <= 8 or
            any(not identifier(s) for s in stores) or len(set(stores)) != len(stores)):
        raise ValueError("invalid storage sources")
    if not isinstance(controller, str) or not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", controller):
        raise ValueError("invalid SATA controller")
    if (not isinstance(ports, (list, tuple)) or len(ports) != 4 or
            any(not isinstance(p, (list, tuple)) or len(p) != 2 or
                type(p[0]) is not int or type(p[1]) is not int or not 1 <= p[1] <= 32 for p in ports)):
        raise ValueError("invalid bay port mapping")
    if sorted(p[0] for p in ports) != [1, 2, 3, 4] or len({p[1] for p in ports}) != 4:
        raise ValueError("bay mapping must be one-to-one")
    if (not isinstance(occupied, (list, tuple)) or any(type(b) is not int or b not in range(1, 5) for b in occupied) or
            len(set(occupied)) != len(occupied)):
        raise ValueError("invalid occupied bays")
    return SiteConfig(node, tuple(vmids), tuple(stores), controller,
                      tuple(sorted(tuple(p) for p in ports)), tuple(sorted(occupied)))


SITE = load_site()


def discover_bays(block_root=Path("/sys/block"), by_id_root=Path("/dev/disk/by-id"),
                  by_path_root=Path("/dev/disk/by-path"), *, site=SITE):
    """Revalidate physical paths every call; never cache device identity."""
    stable = {}
    for link in sorted(by_id_root.glob("ata-*")):
        if "-part" in link.name or not link.is_symlink():
            continue
        try:
            stable.setdefault(link.resolve(strict=True).name, link.name)
        except (OSError, RuntimeError):
            continue
    result = []
    for bay, port in site.bay_ports:
        disk = None
        try:
            target = (by_path_root / f"pci-{site.controller}-ata-{port}.0").resolve(strict=True)
            device = (block_root / target.name / "device").resolve(strict=True)
            if target.name.startswith("sd") and site.controller in device.parts and f"ata{port}" in device.parts:
                disk = stable.get(target.name, f"/dev/{target.name}")
        except (OSError, RuntimeError):
            pass
        result.append((f"BAY {bay}", disk))
    # Ambiguous physical targets must not be attributed to either bay.
    targets = [disk for _, disk in result if disk is not None]
    return tuple((name, disk if targets.count(disk) == 1 else None) for name, disk in result)


def bay_states(disks, *, site=SITE):
    return tuple("present" if disk and bay in site.occupied_bays else
                 "unexpected" if disk else "missing" if bay in site.occupied_bays else "empty"
                 for (bay, _), (_, disk) in zip(site.bay_ports, disks))
