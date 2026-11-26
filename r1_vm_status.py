"""Read-only batch VM state query, without persistent state caching."""
import json
from pathlib import Path
import subprocess
import unicodedata
from r1_site_config import SITE

SELECTION_FILE = Path("/var/lib/r1-web/vm-monitor.json")


def valid_vm_id(value):
    return type(value) is int and 100 <= value <= 999999


def validate_selection(values):
    if (not isinstance(values, list) or len(values) > 64
            or not all(map(valid_vm_id, values)) or len(set(values)) != len(values)):
        raise ValueError("invalid VM selection")
    return tuple(values)


def clean_name(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 40 or any(
            not (unicodedata.category(c)[0] in "LNM" or c in " ._-()·（）") for c in value):
        return ""
    return value


def validate_labels(value):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("invalid VM labels")
    for key, spec in value.items():
        if (not isinstance(key, str) or not key.isascii() or not key.isdecimal()
                or len(key) not in range(3, 7) or not valid_vm_id(int(key))
                or str(int(key)) != key or not isinstance(spec, dict)):
            raise ValueError("invalid VM label")
        mode = spec.get("mode")
        if mode in ("id", "pve") and set(spec) == {"mode"}:
            continue
        raise ValueError("invalid VM display name")
    return value


def monitor_config(default):
    try:
        with SELECTION_FILE.open("rb") as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError("VM selection too large")
        record = json.loads(raw)
        ids = validate_selection(record["vmids"])
        labels = record.get("labels", {})
        if isinstance(labels, dict) and len(labels) <= 64:
            # Earlier development previews allowed aliases. Ignore them without
            # disturbing the saved monitoring selection or other display modes.
            labels = {key: spec for key, spec in labels.items()
                      if not (isinstance(spec, dict) and spec.get("mode") == "custom")}
        try:
            return ids, validate_labels(labels)
        except ValueError:
            return ids, {}
    except FileNotFoundError:
        return tuple(default), {}
    except (OSError, ValueError, KeyError, TypeError):
        return (), {}


def selected_vmids(default):
    return monitor_config(default)[0]


def vm_inventory(names=None):
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
                if names is not None:
                    names.pop(str(vmid), None)
                continue
            status = record.get("status")
            statuses[vmid] = status.upper() if status in ("running", "stopped") else "UNKNOWN"
            name = clean_name(record.get("name"))
            if names is not None and name:
                names[str(vmid)] = name
        return statuses
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def vm_statuses(vmids=None):
    """Compatibility helper for configured IDs."""
    vmids = SITE.vmids if vmids is None else vmids
    inventory = (vm_inventory() or {}) if vmids else {}
    return {v: inventory.get(v, "UNKNOWN") for v in vmids}


