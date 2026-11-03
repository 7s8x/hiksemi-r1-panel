"""Read-only batch VM state query, without persistent state caching."""
import json
import subprocess
from r1_site_config import SITE


def vm_statuses(vmids=None):
    """One local-node API query; preserve qm status' running/stopped meaning."""
    vmids = SITE.vmids if vmids is None else vmids
    statuses = dict.fromkeys(vmids, "UNKNOWN")
    if not vmids:
        return statuses
    try:
        result = subprocess.run(
            ["pvesh", "get", f"/nodes/{SITE.node}/qemu", "--output-format", "json"],
            capture_output=True, text=True, timeout=4, check=True,
        )
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            return statuses
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            vmid = record.get("vmid")
            if type(vmid) is not int or vmid not in statuses:
                continue
            if vmid in seen:
                statuses[vmid] = "UNKNOWN"
                continue
            seen.add(vmid)
            status = record.get("status")
            statuses[vmid] = status.upper() if status in ("running", "stopped") else "UNKNOWN"
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    return statuses


