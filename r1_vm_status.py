"""Read-only batch VM state query, without persistent state caching."""
import json
from pathlib import Path
import subprocess
from r1_site_config import SITE

SELECTION_FILE = Path("/var/lib/r1-web/vm-monitor.json")


def valid_vm_id(value):
    return type(value) is int and 100 <= value <= 999999


def validate_selection(values):
    if (not isinstance(values, list) or len(values) > 64
            or not all(map(valid_vm_id, values)) or len(set(values)) != len(values)):
        raise ValueError("invalid VM selection")
    return tuple(values)


def selected_vmids(default):
    try:
        with SELECTION_FILE.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("VM selection too large")
        return validate_selection(json.loads(raw)["vmids"])
    except FileNotFoundError:
        return tuple(default)
    except (OSError, ValueError, KeyError, TypeError):
        return ()


def vm_inventory():
    """Read local QEMU IDs and statuses in one call; None means query failure."""
    try:
        result = subprocess.run(
            ["pvesh", "get", f"/nodes/{SITE.node}/qemu", "--output-format", "json"],
            capture_output=True, text=True, timeout=4, check=True,
        )
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            return None
        statuses = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            vmid = record.get("vmid")
            if not valid_vm_id(vmid):
                continue
            if vmid in statuses:
                statuses[vmid] = "UNKNOWN"
                continue
            status = record.get("status")
            statuses[vmid] = status.upper() if status in ("running", "stopped") else "UNKNOWN"
        return statuses
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def vm_statuses(vmids=None):
    """Compatibility helper for configured IDs."""
    vmids = SITE.vmids if vmids is None else vmids
    inventory = (vm_inventory() or {}) if vmids else {}
    return {v: inventory.get(v, "UNKNOWN") for v in vmids}


