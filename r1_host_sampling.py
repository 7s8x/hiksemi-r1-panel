"""Read-only host sampling, independent of renderers and display controls."""
from __future__ import annotations

import glob
import os
from pathlib import Path
import re
import subprocess

from r1_site_config import discover_bays, bay_states

_CPU_SAMPLE = None

def discover_drives(block_root=Path("/sys/block"), by_id_root=Path("/dev/disk/by-id"),
                    by_path_root=Path("/dev/disk/by-path")):
    return discover_bays(block_root, by_id_root, by_path_root)


def bay_identity(label, disk):
    if disk is None:
        return label, "UNVERIFIED", "--"
    size, kind, serial = disk_identity(disk)
    return f"{label}  {size}", kind, serial


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def temp_for_hwmon(name):
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_text(path + "/name") == name:
            try:
                return int(read_text(path + "/temp1_input")) / 1000
            except ValueError:
                pass
    return None


def fan_readings(hwmon_root=Path("/sys/class/hwmon")):
    """Read the verified IT8613E fan channels; never change PWM or modes."""
    matches = [path for path in hwmon_root.glob("hwmon*")
               if read_text(path / "name") == "it8613"]
    if len(matches) != 1:
        return None
    path = matches[0]
    values = {}
    for channel in (2, 3):
        for node in (f"fan{channel}_input", f"pwm{channel}", f"pwm{channel}_enable"):
            try:
                values[node] = int(read_text(path / node))
            except ValueError:
                return None
        if not (0 <= values[f"fan{channel}_input"] <= 20000):
            return None
        if not (0 <= values[f"pwm{channel}"] <= 255):
            return None
    return values


def drive_temperature(by_id):
    disk = Path("/dev/disk/by-id", by_id).resolve()
    if not disk.exists():
        return None
    device = Path("/sys/block", disk.name, "device").resolve()
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_text(path + "/name") == "drivetemp" and Path(path, "device").resolve() == device:
            try:
                return int(read_text(path + "/temp1_input")) / 1000
            except ValueError:
                return None
    return None


def memory():
    values = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.strip().split()[0]) / 1024 / 1024
    return values.get("MemTotal", 0), values.get("MemTotal", 0) - values.get("MemAvailable", 0)


def cpu_usage():
    """Compute host CPU busy percentage between two collector samples."""
    global _CPU_SAMPLE
    fields = read_text("/proc/stat").splitlines()
    if not fields or not fields[0].startswith("cpu "):
        return None
    try:
        ticks = [int(value) for value in fields[0].split()[1:]]
    except ValueError:
        return None
    total = sum(ticks)
    idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
    previous, _CPU_SAMPLE = _CPU_SAMPLE, (total, idle)
    if previous is None or total <= previous[0]:
        return None
    return max(0, min(100, round(100 * (1 - (idle - previous[1]) / (total - previous[0])))))


def disk_identity(by_id):
    """Use only the already-configured disk, without probing other devices."""
    disk = Path("/dev/disk/by-id", by_id).resolve()
    name = disk.name
    sectors = read_text(Path("/sys/block", name, "size"))
    try:
        capacity = int(sectors) * 512 / 1_000_000_000
    except ValueError:
        capacity = 0
    size = (f"{capacity / 1000:.0f}TB" if capacity >= 1000
            else f"{capacity:.0f}GB" if capacity else "--")
    kind = ("NVME" if name.startswith("nvme") else
            "HDD" if read_text(Path("/sys/block", name, "queue/rotational")) == "1" else "SSD")
    serial = by_id.rsplit("_", 1)[-1] if "_" in by_id else "--"
    return size, kind, serial


def nvme_identity():
    devices = sorted(Path("/sys/block").glob("nvme*n*"))
    if not devices:
        return "--", "NVME", "--"
    name = devices[0].name
    sectors = read_text(devices[0] / "size")
    try:
        capacity = int(sectors) * 512 / 1_000_000_000
    except ValueError:
        capacity = 0
    size = f"{capacity / 1000:.0f}TB" if capacity >= 1000 else f"{capacity:.0f}GB" if capacity else "--"
    controller = name.rsplit("n", 1)[0]
    return size, "NVME", read_text(Path("/sys/class/nvme", controller, "serial")) or "--"




def drive_models(disks: tuple) -> list[str]:
    """Read model fields only; never derive a display label from by-id serials."""
    try:
        controller = next(iter(sorted(Path("/sys/class/nvme").glob("nvme*"))))
        nvme = (controller / "model").read_text(encoding="utf-8").strip()
    except (OSError, StopIteration):
        nvme = "--"
    models = [nvme]
    for _, disk in disks:
        if disk is None:
            models.append("--")
            continue
        try:
            block = (Path("/dev/disk/by-id") / disk).resolve(strict=True).name
            model = (Path("/sys/block") / block / "device/model").read_text(encoding="utf-8").strip()
        except (OSError, RuntimeError):
            model = "--"
        models.append(model)
    return [re.sub(r"\s+", " ", value)[:24].strip() or "--" for value in models]




def system_identity() -> tuple[str, str]:
    """Return short, non-secret host labels for the public Web headers."""
    try:
        result = subprocess.run(["pveversion"], capture_output=True, text=True,
                                check=True, timeout=3)
        version = re.search(r"pve-manager/([0-9]+(?:\.[0-9]+){1,2})", result.stdout)
    except (OSError, subprocess.SubprocessError):
        version = None
    pve = "PROXMOX VE " + (version.group(1) if version else "--")
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
    except OSError:
        cpuinfo = ""
    model = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    cores = re.search(r"^cpu cores\s*:\s*([0-9]+)$", cpuinfo, re.MULTILINE)
    label = re.sub(r"\s+", " ", re.sub(r"\((?:R|TM)\)", "", model.group(1))).strip() if model else "CPU"
    label = label[:18].rstrip()
    threads = os.cpu_count()
    detail = f" · {cores.group(1)}C / {threads}T" if cores and threads else ""
    return pve, label + detail
