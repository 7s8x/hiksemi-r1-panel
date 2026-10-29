#!/usr/bin/env python3
"""PVE status dashboard and local screen controls for the HIKSEMI R1."""

import argparse
import ctypes
import fcntl
import glob
import json
import mmap
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import time

from r1_fan_control_ipc import control_status, request_mode
from r1_rgb_control import RgbControl
from r1_panel_runtime import MAX_COMMAND, SnapshotCollector, open_control, publish_state
from r1_storage_usage import storage_usage
from r1_web_capacity import storage_capacity
from r1_web_views import drive_models, render_fans, render_information, system_identity
from r1_fan_curve import temperatures as fan_temperatures
from r1_svg_painter import paint_svg


WIDTH = 376
HEIGHT = 960
BAY_PORTS = ((1, 4), (2, 3), (3, 2), (4, 1))
BAY_CONTROLLER = "0000:04:00.0"


def discover_drives(block_root=Path("/sys/block"), by_id_root=Path("/dev/disk/by-id"),
                    by_path_root=Path("/dev/disk/by-path")):
    """Return all four R1 bays in physical order; an absent path is unverified."""
    stable_ids = {}
    for link in sorted(by_id_root.glob("ata-*")):
        if "-part" in link.name or not link.is_symlink():
            continue
        try:
            stable_ids.setdefault(link.resolve(strict=True).name, link.name)
        except (OSError, RuntimeError):
            continue
    found = []
    for bay, port in BAY_PORTS:
        link = by_path_root / f"pci-{BAY_CONTROLLER}-ata-{port}.0"
        disk = None
        try:
            name = link.resolve(strict=True).name
            block = block_root / name
            device = (block / "device").resolve(strict=True)
            if (name.startswith("sd") and block.is_dir() and
                    BAY_CONTROLLER in device.parts and f"ata{port}" in device.parts):
                disk = stable_ids.get(name, f"/dev/{name}")
        except (OSError, RuntimeError):
            pass
        found.append((f"BAY {bay}", disk))
    return tuple(found)


def bay_identity(label, disk):
    if disk is None:
        return label, "UNVERIFIED", "--"
    size, kind, serial = disk_identity(disk)
    return f"{label}  {size}", kind, serial


STOP = False
_CPU_SAMPLE = None
# Keep console opposite the dark page: switching directly from a black fbdev
# frame to tty1 showed only a cursor in the first guarded four-page trial.
SCREEN_STATES = ("panel", "fans", "console", "off")
RGB = RgbControl()


def rgb_status():
    try:
        return RGB.status()
    except (OSError, RuntimeError, ValueError):
        return None


def stop(_signum, _frame):
    global STOP
    STOP = True


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


def vm_status(vmid):
    try:
        result = subprocess.run(
            ["qm", "status", str(vmid)], capture_output=True, text=True,
            timeout=4, check=True,
        )
        return result.stdout.strip().removeprefix("status: ").upper()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def network_address():
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", "vmbr0"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        for part in result.stdout.split():
            if part.startswith("192.") or part.startswith("10.") or part.startswith("172."):
                return part.split("/")[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return "--"


def snapshot():
    total, used = memory()
    disks = discover_drives()
    usage = storage_usage(disks)
    disk_readings = [(label, bay_identity(label, disk),
                      drive_temperature(disk) if disk is not None else None)
                     for label, disk in disks]
    try:
        uptime = int(float(read_text("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        uptime = 0
    return {
        "cpu": temp_for_hwmon("coretemp"),
        "cpu_usage": cpu_usage(),
        "nvme": temp_for_hwmon("nvme"),
        "drives": [(label, temperature) for label, identity, temperature in disk_readings
                   if identity[1] != "UNVERIFIED"],
        "storage": [(nvme_identity(), temp_for_hwmon("nvme"))] +
                   [(identity, temperature) for _, identity, temperature in disk_readings],
        "storage_usage": usage,
        "mem_total": total,
        "mem_used": used,
        "uptime": uptime,
        "ip": network_address(),
        "vms": [(100, vm_status(100)), (111, vm_status(111))],
        "fans": fan_readings(),
        "control": control_status(),
        "rgb": rgb_status(),
        "captured_at": time.monotonic(),
        "clock": time.strftime("%H:%M"),
    }


def screen_snapshot():
    """Collect the additional display-only fields used by the accepted Web UI."""
    data = snapshot()
    disks = discover_drives()
    data["storage_models"] = drive_models(disks)
    data["storage_capacity"] = storage_capacity(disks)
    data["pve_version"], data["cpu_model"] = system_identity()
    data["fan_basis"] = fan_temperatures()
    data["vms"] = [(f"VM {vmid}", status) for vmid, status in data["vms"]]
    return data


class Painter:
    def __init__(self):
        self.lib = ctypes.CDLL("libcairo.so.2")
        specs = {
            "cairo_image_surface_create": ([ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
            "cairo_create": ([ctypes.c_void_p], ctypes.c_void_p),
            "cairo_destroy": ([ctypes.c_void_p], None),
            "cairo_surface_destroy": ([ctypes.c_void_p], None),
            "cairo_set_source_rgb": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double], None),
            "cairo_rectangle": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double], None),
            "cairo_new_sub_path": ([ctypes.c_void_p], None),
            "cairo_arc": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double], None),
            "cairo_close_path": ([ctypes.c_void_p], None),
            "cairo_fill": ([ctypes.c_void_p], None),
            "cairo_move_to": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double], None),
            "cairo_select_font_face": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int], None),
            "cairo_set_font_size": ([ctypes.c_void_p, ctypes.c_double], None),
            "cairo_show_text": ([ctypes.c_void_p, ctypes.c_char_p], None),
            "cairo_text_extents": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p], None),
            "cairo_surface_flush": ([ctypes.c_void_p], None),
            "cairo_image_surface_get_data": ([ctypes.c_void_p], ctypes.c_void_p),
            "cairo_image_surface_get_stride": ([ctypes.c_void_p], ctypes.c_int),
            "cairo_surface_write_to_png": ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_int),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            fn.restype = result
        self.surface = self.lib.cairo_image_surface_create(0, WIDTH, HEIGHT)
        self.ctx = self.lib.cairo_create(self.surface)

    def color(self, rgb):
        self.lib.cairo_set_source_rgb(self.ctx, *(c / 255 for c in rgb))

    def rect(self, x, y, w, h, rgb):
        self.color(rgb)
        self.lib.cairo_rectangle(self.ctx, x, y, w, h)
        self.lib.cairo_fill(self.ctx)

    def round_rect(self, x, y, w, h, rgb, radius=6):
        self.color(rgb)
        r = min(radius, w / 2, h / 2)
        self.lib.cairo_new_sub_path(self.ctx)
        for cx, cy, start in ((x + w - r, y + r, -1.5707963268),
                              (x + w - r, y + h - r, 0),
                              (x + r, y + h - r, 1.5707963268),
                              (x + r, y + r, 3.1415926536)):
            self.lib.cairo_arc(self.ctx, cx, cy, r, start, start + 1.5707963268)
        self.lib.cairo_close_path(self.ctx)
        self.lib.cairo_fill(self.ctx)

    def text(self, x, y, value, size=18, rgb=(222, 235, 240), bold=False):
        self.color(rgb)
        self.lib.cairo_select_font_face(self.ctx, b"DejaVu Sans", 0, 1 if bold else 0)
        self.lib.cairo_set_font_size(self.ctx, size)
        self.lib.cairo_move_to(self.ctx, x, y)
        self.lib.cairo_show_text(self.ctx, str(value).encode("utf-8"))

    def text_right(self, right, y, value, size=18, rgb=(222, 235, 240), bold=False):
        self.lib.cairo_select_font_face(self.ctx, b"DejaVu Sans", 0, 1 if bold else 0)
        self.lib.cairo_set_font_size(self.ctx, size)
        extents = (ctypes.c_double * 6)()
        self.lib.cairo_text_extents(self.ctx, str(value).encode("utf-8"), extents)
        self.text(right - extents[4], y, value, size, rgb, bold)

    def draw(self, data, touch_mode=False, brightness=None, view="panel"):
        svg = render_fans(data) if view == "fans" else render_information(data)
        paint_svg(self, svg)
        return
        bg, panel, muted, fg, cyan = (9, 20, 29), (19, 40, 52), (145, 169, 180), (231, 242, 246), (103, 185, 208)
        self.rect(0, 0, WIDTH, HEIGHT, bg)
        self.text(24, 42, "HIKSEMI R1  /  PVE", 14, cyan, True)
        self.text(24, 81, "HOST OVERVIEW", 27, fg, True)
        self.text(289, 110, data["clock"], 18, muted)
        self.rect(24, 106, 9, 9, (112, 213, 183))
        self.text(42, 117, "HOST ONLINE", 14, (112, 213, 183))

        self.rect(20, 140, 336, 182, panel)
        self.text(40, 172, "CPU PACKAGE", 15, muted)
        cpu = data["cpu"]
        self.text(38, 261, "--" if cpu is None else f"{cpu:.0f}", 78, fg, True)
        self.text(165, 261, "C", 27, fg)
        self.rect(40, 281, 296, 6, (31, 56, 68))
        if cpu is not None:
            self.rect(40, 281, min(296, 296 * max(0, cpu) / 105), 6, (247, 173, 109))
        stale = time.monotonic() - data.get("captured_at", 0) > 30
        self.text(40, 307, "DATA STALE / CHECK COLLECTOR" if stale else "LIVE SENSOR / 15 SEC REFRESH",
                  12, (247, 173, 109) if stale else muted)

        self.text(24, 366, "HARDWARE TEMPERATURE", 17, fg, True)
        readings = [("NVME SYSTEM", data["nvme"]), *data["drives"]]
        for idx, (label, temp) in enumerate(readings):
            y = 384 + idx * 66
            self.rect(20, y, 336, 56, panel)
            self.text(36, y + 24, label, 15, fg, True)
            self.text(36, y + 44, "SENSOR UNAVAILABLE" if temp is None else "CURRENT TEMPERATURE", 10, muted)
            self.text(276, y + 35, "--" if temp is None else f"{temp:.0f}C", 21, cyan, True)

        self.text(24, 760, "SYSTEM", 17, fg, True)
        self.rect(20, 778, 161, 112, panel)
        self.rect(195, 778, 161, 112, panel)
        self.text(34, 804, "MEMORY", 12, muted)
        self.text(34, 841, f"{data['mem_used']:.1f} / {data['mem_total']:.0f}", 22, fg, True)
        self.text(34, 867, "GiB USED", 11, muted)
        self.text(209, 804, "VM STATUS", 12, muted)
        for idx, (vmid, status) in enumerate(data["vms"]):
            self.text(209, 833 + idx * 25, f"{vmid}  {status}", 13, (112, 213, 183) if status == "RUNNING" else fg, True)
        self.text(24, 918, f"{data['ip']}  /  UP {data['uptime'] // 3600}H {data['uptime'] % 3600 // 60}M", 15, muted)
        if touch_mode:
            self.text(24, 942, f"SWIPE L/R: VIEW    UP/DOWN: {brightness}%", 9, muted)
        else:
            self.text(24, 942, "DISPLAY ONLY  -  NO TOUCH ACTIONS", 10, muted)
        self.lib.cairo_surface_flush(self.surface)

    def draw_fans(self, data, touch_mode=False, brightness=None):
        bg, panel, muted, fg = (9, 20, 29), (19, 40, 52), (145, 169, 180), (231, 242, 246)
        cyan, green, amber = (103, 185, 208), (112, 213, 183), (247, 173, 109)
        fans = data.get("fans")
        stale = time.monotonic() - data.get("captured_at", 0) > 30
        valid = fans is not None and not stale
        stopped = valid and any(fans[f"fan{channel}_input"] == 0 for channel in (2, 3))
        status_color = green if valid and not stopped else amber
        status = "FAN RPM ZERO" if stopped else "IT8613E / LIVE" if valid else "DATA STALE / OFFLINE"
        self.rect(0, 0, WIDTH, HEIGHT, bg)
        self.text(24, 42, "HIKSEMI R1  /  PVE", 14, cyan, True)
        self.text(24, 81, "FAN MONITOR", 27, fg, True)
        self.text(292, 108, data["clock"], 18, muted)
        self.rect(24, 105, 9, 9, status_color)
        self.text(42, 117, status, 14, status_color)

        control = data.get("control") or {}
        for index, channel in enumerate((2, 3)):
            y = 142 + index * 225
            rpm = fans.get(f"fan{channel}_input") if valid else None
            pwm = fans.get(f"pwm{channel}") if valid else None
            mode = fans.get(f"pwm{channel}_enable") if valid else None
            self.rect(20, y, 336, 208, panel)
            self.text(40, y + 30, f"FAN {channel}", 18, fg, True)
            self.text(40, y + 112, "--" if rpm is None else str(rpm), 62, green if rpm and rpm > 0 else amber, True)
            self.text(260, y + 111, "RPM", 18, muted)
            self.rect(40, y + 133, 296, 2, (31, 56, 68))
            self.text(40, y + 166, "PWM RAW --" if pwm is None else f"PWM RAW {pwm}", 15, fg)
            mode_text = {1: "MANUAL PWM", 2: "HARDWARE AUTO"}.get(mode, "MODE --")
            if mode == 1 and control.get("live"):
                mode_text = ("SW AUTO / MANUAL PWM" if control.get("mode") == "auto"
                             else "FULL / MANUAL PWM")
            self.text(40, y + 192, mode_text, 13, muted)

        live = bool(control.get("live"))
        desired, actual = control.get("desired"), control.get("mode")
        if not live:
            control_text, control_color = "CONTROLLER OFFLINE", amber
        elif desired != actual:
            control_text, control_color = "APPLYING MODE...", amber
        else:
            remaining = control.get("remaining")
            control_text = (f"FULL SPEED {remaining}S LEFT" if actual == "full" and remaining is not None
                            else "FULL SPEED ACTIVE" if actual == "full" else "AUTO CURVE ACTIVE")
            control_color = amber if actual == "full" else green
        self.rect(20, 605, 336, 142, panel)
        self.text(40, 636, "FAN CONTROL", 17, cyan, True)
        self.text(40, 661, control_text, 14, control_color, True)
        for mode, label, x, active_color in (("full", "FULL SPEED", 30, amber),
                                              ("auto", "AUTO CURVE", 196, green)):
            selected = live and desired == mode
            self.rect(x, 678, 150, 54, active_color if selected else (31, 56, 68))
            self.text(x + 12, 711, label, 14, bg if selected else fg, True)

        self.text(24, 774, "THERMAL  /  LIGHT", 17, fg, True)
        drive_temps = [temp for _, temp in data["drives"] if temp is not None]
        cpu_text = "--" if data["cpu"] is None else f"{data['cpu']:.0f}C"
        drive_text = "--" if not drive_temps else f"{max(drive_temps):.0f}C"
        self.text(24, 801, f"CPU {cpu_text}     MAX DRIVE {drive_text}", 13, muted)
        self.rect(20, 817, 336, 73, panel)
        rgb = data.get("rgb")
        rgb_text = "UNAVAILABLE" if rgb is None else "ON" if rgb else "OFF"
        self.text(36, 843, "R1 RGB LIGHT", 15, fg, True)
        self.text(36, 869, f"STATUS: {rgb_text}", 12, amber if rgb is None else green if rgb else muted)
        self.rect(227, 827, 113, 53, (31, 56, 68) if rgb is None else amber if rgb else green)
        self.text(240, 860, "UNAVAIL" if rgb is None else "TURN OFF" if rgb else "TURN ON", 13, bg if rgb is not None else muted, True)
        self.text(24, 918, f"{data['ip']}  /  UP {data['uptime'] // 3600}H {data['uptime'] % 3600 // 60}M", 15, muted)
        if touch_mode:
            self.text(24, 942, f"SWIPE L/R: VIEW    UP/DOWN: {brightness}%", 9, muted)
        else:
            self.text(24, 942, "DISPLAY ONLY  -  NO TOUCH ACTIONS", 10, muted)
        self.lib.cairo_surface_flush(self.surface)

    def draw_locked_header(self, data, title, status, status_color):
        bg, fg, cyan, muted = (9, 20, 29), (231, 242, 246), (103, 185, 208), (145, 169, 180)
        self.rect(0, 0, WIDTH, HEIGHT, bg)
        self.text(24, 42, "HIKSEMI R1  /  PVE", 14, cyan, True)
        self.text(24, 80, title, 27, fg, True)
        self.rect(24, 104, 9, 9, status_color)
        self.text(42, 115, status, 14, status_color)
        self.text(24, 174, data.get("clock", "--:--"), 43, fg, True)
        self.text(240, 143, "UPTIME", 12, muted)
        uptime = max(0, data.get("uptime") or 0)
        self.text(240, 171, f"{uptime // 86400}D {uptime % 86400 // 3600}H", 24, cyan, True)

    def draw_locked_panel(self, data):
        panel, muted, fg = (19, 40, 52), (145, 169, 180), (231, 242, 246)
        cyan, green, amber = (103, 185, 208), (112, 213, 183), (247, 173, 109)
        self.draw_locked_header(data, "HOST OVERVIEW", "HOST ONLINE", green)
        self.round_rect(20, 194, 336, 142, panel)
        self.rect(187, 212, 1, 88, (41, 65, 79))
        self.text(40, 222, "CPU USAGE", 13, muted)
        self.text(207, 222, "CPU TEMP", 13, muted)
        usage, cpu = data.get("cpu_usage"), data.get("cpu")
        self.text(38, 275, "--" if usage is None else f"{usage}%", 49, fg, True)
        self.text(204, 275, "--" if cpu is None else f"{cpu:.0f}", 49, fg, True)
        self.text(288, 274, "°C", 22, fg)
        self.rect(40, 295, 127, 6, (31, 56, 68))
        self.rect(207, 295, 129, 6, (31, 56, 68))
        if usage is not None:
            self.rect(40, 295, round(127 * max(0, min(100, usage)) / 100), 6, green)
        if cpu is not None:
            self.rect(207, 295, round(129 * max(0, min(105, cpu)) / 105), 6, amber)
        stale = time.monotonic() - data.get("captured_at", 0) > 30
        self.text(40, 324, "DATA STALE / CHECK COLLECTOR" if stale else "LIVE DATA / 15 SEC REFRESH",
                  11, amber if stale else muted)
        self.text(24, 367, "HARDWARE TEMPERATURE", 17, fg, True)
        storage = data.get("storage") or []
        storage_usage_values = data.get("storage_usage") or []
        for index, (identity, temperature) in enumerate(storage[:5]):
            size, kind, _ = identity
            y = 384 + index * 80
            self.round_rect(20, y, 336, 76, panel)
            self.text(36, y + 30, size if kind == "UNVERIFIED" else f"{size}  {kind}", 18, fg, True)
            percent = storage_usage_values[index] if index < len(storage_usage_values) else None
            if kind == "UNVERIFIED":
                self.text(36, y + 57, "NO DRIVE / CHECK PATH", 11, muted)
            else:
                self.text(36, y + 56, "USED", 11, muted)
                self.text(78, y + 57, "--" if percent is None else f"{percent}%",
                          16, muted if percent is None else green, True)
                self.rect(36, y + 65, 146, 4, (31, 56, 68))
                if percent is not None:
                    self.rect(36, y + 65, round(146 * max(0, min(100, percent)) / 100), 4, green)
            self.text_right(303, y + 50, "--" if temperature is None else f"{temperature:.0f}", 34, cyan, True)
            self.text(307, y + 49, "°C", 17, cyan, True)
        self.text(24, 808, "SYSTEM", 17, fg, True)
        self.round_rect(20, 826, 161, 119, panel)
        self.round_rect(195, 826, 161, 119, panel)
        self.text(34, 852, "VM STATUS", 12, muted)
        vm_names = {100: "Synology", 111: "ImmortalWrt"}
        for index, (vmid, status) in enumerate(data.get("vms") or []):
            if index >= 2:
                break
            y = 883 + index * 27
            color = green if status == "RUNNING" else muted
            self.text(34, y, vm_names.get(vmid, str(vmid)), 14, color, True)
            self.text_right(167, y, "ON" if status == "RUNNING" else "OFF" if status == "STOPPED" else "--", 12, color, True)
        self.text(209, 852, "MEMORY USED", 12, muted)
        used, total = data.get("mem_used") or 0, data.get("mem_total") or 0
        self.text(209, 898, f"{used:.1f}", 32, fg, True)
        self.text_right(342, 898, f"/ {total:.0f} GiB", 12, muted)
        self.rect(209, 922, 133, 5, (31, 56, 68))
        if total > 0:
            self.rect(209, 922, round(133 * min(1, used / total)), 5, cyan)
        self.lib.cairo_surface_flush(self.surface)

    def draw_locked_fans(self, data):
        panel, muted, fg = (19, 40, 52), (145, 169, 180), (231, 242, 246)
        cyan, green, amber, bg = (103, 185, 208), (112, 213, 183), (247, 173, 109), (9, 20, 29)
        fans = data.get("fans")
        valid = fans is not None and time.monotonic() - data.get("captured_at", 0) <= 30
        stopped = valid and any(fans[f"fan{channel}_input"] == 0 for channel in (2, 3))
        status_color = green if valid and not stopped else amber
        status = "FAN RPM ZERO" if stopped else "IT8613E / LIVE" if valid else "DATA STALE / OFFLINE"
        self.draw_locked_header(data, "FAN MONITOR", status, status_color)
        control = data.get("control") or {}
        for index, (name, channel) in enumerate((("SYS FAN", 2), ("CPU FAN", 3))):
            y = 194 + index * 210
            rpm = fans.get(f"fan{channel}_input") if valid else None
            pwm = fans.get(f"pwm{channel}") if valid else None
            self.round_rect(20, y, 336, 197, panel)
            self.text(40, y + 33, name, 19, fg, True)
            self.text_right(336, y + 33, f"FAN {channel}", 12, muted)
            self.text(40, y + 108, "--" if rpm is None else str(rpm), 66,
                      green if rpm and rpm > 0 else amber, True)
            self.text_right(336, y + 105, "RPM", 17, muted)
            self.rect(40, y + 126, 296, 2, (41, 65, 79))
            percent = "--" if pwm is None else str(round(pwm * 100 / 255))
            self.text(40, y + 160, f"OUTPUT {percent}%", 18, fg)
            self.text(40, y + 183, "SW AUTO / MANUAL PWM", 12, muted)
        live = bool(control.get("live"))
        desired, actual = control.get("desired"), control.get("mode")
        if not live:
            control_text, control_color = "CONTROLLER OFFLINE", amber
        elif desired != actual:
            control_text, control_color = "APPLYING MODE...", amber
        else:
            remaining = control.get("remaining")
            control_text = (f"FULL SPEED {remaining}S LEFT" if actual == "full" and remaining is not None
                            else "FULL SPEED ACTIVE" if actual == "full" else "AUTO CURVE ACTIVE")
            control_color = amber if actual == "full" else green
        self.round_rect(20, 635, 336, 127, panel)
        self.text(40, 663, "FAN CONTROL", 18, cyan, True)
        self.text(40, 690, control_text, 15, control_color, True)
        for mode, label, x, selected_color in (("full", "FULL SPEED", 30, amber),
                                                ("auto", "AUTO CURVE", 196, green)):
            selected = live and desired == mode
            self.round_rect(x, 706, 150, 44, selected_color if selected else (32, 61, 77), 4)
            self.text(x + 13, 734, label, 14, bg if selected else fg, True)
        self.text(24, 783, "THERMAL / LIGHT", 17, fg, True)
        drive_temps = [temp for _, temp in data.get("drives") or [] if temp is not None]
        cpu = data.get("cpu")
        self.text(24, 807, f"CPU {'--' if cpu is None else f'{cpu:.0f}°C'}   MAX DRIVE "
                  f"{'--' if not drive_temps else f'{max(drive_temps):.0f}°C'}", 13, muted)
        self.round_rect(20, 825, 336, 127, panel)
        rgb = data.get("rgb")
        self.text(40, 853, "R1 RGB LIGHT", 18, cyan, True)
        self.text(40, 880, "LIGHT UNAVAILABLE" if rgb is None else "LIGHT ON" if rgb else "LIGHT OFF",
                  15, amber if rgb is None else green if rgb else muted, True)
        for action, label, x in (("on", "TURN ON", 30), ("off", "TURN OFF", 196)):
            selected = rgb is not None and rgb == (action == "on")
            self.round_rect(x, 896, 150, 44, green if selected else (32, 61, 77), 4)
            self.text(x + 13, 924, label, 14, bg if selected else fg, True)
        self.lib.cairo_surface_flush(self.surface)

    def end_screen(self):
        self.rect(0, 0, WIDTH, HEIGHT, (9, 20, 29))
        self.text(24, 82, "HIKSEMI R1  /  PVE", 16, (103, 185, 208), True)
        self.text(24, 430, "PREVIEW ENDED", 26, (231, 242, 246), True)
        self.text(24, 466, "Live data is no longer shown.", 14, (145, 169, 180))
        self.text(24, 925, "SSH AND PVE SERVICES ARE UNCHANGED", 11, (145, 169, 180))
        self.lib.cairo_surface_flush(self.surface)

    def pixels(self):
        stride = self.lib.cairo_image_surface_get_stride(self.surface)
        pointer = self.lib.cairo_image_surface_get_data(self.surface)
        return ctypes.string_at(pointer, stride * HEIGHT), stride

    def png(self, path):
        status = self.lib.cairo_surface_write_to_png(self.surface, os.fsencode(path))
        if status:
            raise RuntimeError(f"PNG write failed: Cairo status {status}")

    def close(self):
        self.lib.cairo_destroy(self.ctx)
        self.lib.cairo_surface_destroy(self.surface)


def framebuffer_info(fd):
    var = bytearray(160)
    fix = bytearray(128)
    fcntl.ioctl(fd, 0x4600, var, True)
    fcntl.ioctl(fd, 0x4602, fix, True)
    xres, yres, _, _, _, _, bpp, _ = struct.unpack_from("8I", var)
    red, green, blue = struct.unpack_from("I", var, 32)[0], struct.unpack_from("I", var, 44)[0], struct.unpack_from("I", var, 56)[0]
    line_length = struct.unpack_from("I", fix, 48)[0]
    if (xres, yres, bpp, red, green, blue) != (WIDTH, HEIGHT, 32, 16, 8, 0):
        raise RuntimeError(f"Unsupported framebuffer: {xres}x{yres} {bpp}bpp RGB offsets {red}/{green}/{blue}")
    if line_length < WIDTH * 4:
        raise RuntimeError(f"Invalid framebuffer stride: {line_length}")
    return line_length


def touchscreen_path():
    for path in glob.glob("/sys/class/input/event*"):
        if read_text(path + "/device/name") == "CST0836:00 0911:5288":
            return "/dev/input/" + Path(path).name
    raise RuntimeError("R1 touchscreen was not found")


class TouchGesture:
    """Recognize one-finger taps and horizontal/vertical swipes."""

    EVENT_SIZE = struct.calcsize("llHHi")

    def __init__(self):
        self.x = 0
        self.y = 0
        self.down = False
        self.start = None
        self.finish = False
        self.begin = False
        self.multi = False
        self.slot = 0
        self.buffer = b""

    def feed(self, chunk):
        self.buffer += chunk
        gestures = []
        while len(self.buffer) >= self.EVENT_SIZE:
            item, self.buffer = self.buffer[:self.EVENT_SIZE], self.buffer[self.EVENT_SIZE:]
            _, _, kind, code, value = struct.unpack("llHHi", item)
            if kind == 3:  # EV_ABS
                if code == 0:  # ABS_X
                    self.x = value
                elif code == 1:  # ABS_Y
                    self.y = value
                elif code == 0x2F:  # ABS_MT_SLOT
                    self.slot = value
                    if value > 0 and self.down:
                        self.multi = True
                elif code == 0x35 and self.slot == 0:  # ABS_MT_POSITION_X
                    self.x = value
                elif code == 0x36 and self.slot == 0:  # ABS_MT_POSITION_Y
                    self.y = value
            elif kind == 1 and code == 0x14A:  # BTN_TOUCH
                if value:
                    self.begin = True
                else:
                    self.finish = True
            elif kind == 0 and code == 0:  # SYN_REPORT
                if self.begin:
                    self.down = True
                    self.start = (self.x, self.y, time.monotonic())
                    self.multi = False
                    self.begin = False
                if self.finish:
                    if self.down and self.start and not self.multi:
                        sx, sy, started = self.start
                        dx, dy = self.x - sx, self.y - sy
                        duration = time.monotonic() - started
                        if duration <= 1.5 and abs(dx) >= 80 and abs(dx) > abs(dy) * 1.3:
                            gestures.append(("swipe_horizontal", dx))
                        elif duration <= 1.5 and abs(dy) >= 80 and abs(dy) > abs(dx) * 1.3:
                            gestures.append(("swipe_vertical", dy))
                        elif duration <= 0.9 and abs(dx) <= 35 and abs(dy) <= 35:
                            gestures.append(("tap", (self.x, self.y)))
                    self.down = False
                    self.start = None
                    self.finish = False
        return gestures


def switch_vt(number):
    subprocess.run(["chvt", str(number)], check=True, timeout=3)


def set_tty2_cursor(visible):
    """Hide only the panel VT's fbcon cursor; never change global fbcon mode."""
    try:
        fd = os.open("/dev/tty2", os.O_WRONLY | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            os.write(fd, b"\x1b[?25h" if visible else b"\x1b[?25l")
        finally:
            os.close(fd)
    except OSError as error:
        print(f"tty2_cursor_error={error}", flush=True)


def shifted_state(current, horizontal_delta):
    """Swipe left for the next view, right for the previous view."""
    step = 1 if horizontal_delta < 0 else -1
    return SCREEN_STATES[(SCREEN_STATES.index(current) + step) % len(SCREEN_STATES)]


def fan_button_mode(point):
    x, y = point
    if 738 <= y <= 783:
        if 32 <= x <= 180:
            return "full"
        if 196 <= x <= 344:
            return "auto"
    return None


def rgb_button_action(point):
    x, y = point
    if 882 <= y <= 927:
        if 32 <= x <= 180:
            return "on"
        if 196 <= x <= 344:
            return "off"
    return None


def run_touch(painter, initial_view="panel"):
    backlight = Path("/sys/class/backlight/intel_backlight")
    fb_blank = Path("/sys/class/graphics/fb0/blank")
    maximum = int(read_text(backlight / "max_brightness"))
    brightness = int(read_text(backlight / "brightness"))
    if maximum <= 0:
        raise RuntimeError("Backlight maximum is invalid")
    brightness = max(round(maximum * 0.02), brightness)
    minimum = max(1, round(maximum * 0.02))
    step = max(1, round(maximum * 0.05))
    touch_fd = os.open(touchscreen_path(), os.O_RDONLY | os.O_NONBLOCK)
    fb_fd = os.open("/dev/fb0", os.O_RDWR)
    collector = SnapshotCollector(screen_snapshot)
    listener = open_control()
    collector.start()
    try:
        line_length = framebuffer_info(fb_fd)
        gesture = TouchGesture()
        state = initial_view
        next_refresh = 0
        last_data = None
        last_sequence = -1
        with mmap.mmap(fb_fd, line_length * HEIGHT, mmap.MAP_SHARED, mmap.PROT_WRITE) as framebuffer:
            def blit():
                pixels, stride = painter.pixels()
                # The i915 fbdev write path reports damaged regions to DRM.
                # Direct mmap stores may leave FBC displaying an old frame.
                frame = bytearray(line_length * HEIGHT)
                for y in range(HEIGHT):
                    frame[y * line_length:y * line_length + WIDTH * 4] = pixels[y * stride:y * stride + WIDTH * 4]
                written = 0
                while written < len(frame):
                    count = os.pwrite(fb_fd, memoryview(frame)[written:], written)
                    if count <= 0:
                        raise OSError("short framebuffer write")
                    written += count

            def refresh(use_cache=False, view=None):
                nonlocal last_data, last_sequence
                if use_cache and last_data is not None:
                    data = dict(last_data)
                    data["clock"] = time.strftime("%H:%M")
                else:
                    data, last_sequence = collector.latest()
                    last_data = data
                if (view or state) == "fans":
                    data["control"] = control_status()
                painter.draw(data, touch_mode=True, brightness=round(100 * brightness / maximum), view=view or state)
                blit()
                # getty@tty2 can start after the first chvt and re-enable its
                # text cursor. Reassert the per-VT hide after every redraw.
                set_tty2_cursor(False)

            def show_dashboard(target):
                if state in ("panel", "fans"):
                    refresh(use_cache=True, view=target)
                    return
                if state == "off":
                    # Keep tty2 and the DSI link active while the backlight is
                    # dark. Re-enabling fb_blank can freeze this panel on a
                    # stale tty1 frame (i915 video_mode == 3 warning).
                    refresh(use_cache=True, view=target)
                    (backlight / "brightness").write_text(str(brightness))
                    blit()
                    return
                # Keep the LCD dark while chvt/fbcon and fb_blank redraw the
                # framebuffer. Reveal it only after our own frame is ready.
                (backlight / "brightness").write_text("0")
                switch_vt(2)
                set_tty2_cursor(False)
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                time.sleep(0.12)
                refresh(use_cache=True, view=target)
                (backlight / "brightness").write_text(str(brightness))
                blit()

            def show_console():
                switch_vt(1)
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))

            def show_off():
                # Paint black and extinguish the backlight without resetting
                # the DSI link; blank/unblank can leave the LCD scanout stale.
                # If tty1 owns fb0, blacking it out and then calling chvt 1
                # again cannot redraw the console. Keep the dark page on tty2.
                if state == "console":
                    (backlight / "brightness").write_text("0")
                    switch_vt(2)
                    set_tty2_cursor(False)
                painter.rect(0, 0, WIDTH, HEIGHT, (0, 0, 0))
                painter.lib.cairo_surface_flush(painter.surface)
                blit()
                (backlight / "brightness").write_text("0")

            def transition(target):
                nonlocal state, next_refresh
                if target == state:
                    return
                if target in ("panel", "fans"):
                    show_dashboard(target)
                    next_refresh = time.monotonic() + 0.2
                elif target == "console":
                    show_console()
                else:
                    show_off()
                state = target

            def handle_action(action, value):
                nonlocal brightness, next_refresh
                if action == "swipe_vertical":
                    increments = max(1, min(5, round(abs(value) / 100)))
                    brightness = min(maximum, max(minimum, brightness + (step * increments if value < 0 else -step * increments)))
                    if state != "off":
                        (backlight / "brightness").write_text(str(brightness))
                    if state in ("panel", "fans"):
                        refresh(use_cache=True)
                elif action == "swipe_horizontal":
                    transition(shifted_state(state, value))
                elif action == "tap":
                    mode = fan_button_mode(value) if state == "fans" else None
                    rgb_action = rgb_button_action(value) if state == "fans" else None
                    if rgb_action is not None:
                        enabled = RGB.status()
                        if enabled != (rgb_action == "on"):
                            enabled = RGB.toggle()
                        if last_data is not None:
                            last_data["rgb"] = enabled
                        refresh(use_cache=True)
                        next_refresh = time.monotonic() + 1
                    elif mode is not None:
                        if not control_status()["live"]:
                            raise RuntimeError("fan controller offline")
                        request_mode(mode)
                        refresh(use_cache=True)
                        next_refresh = time.monotonic() + 1
                else:
                    raise ValueError("unknown panel action")

            def handle_command(command):
                if not isinstance(command, dict) or set(command) != {"action", "value"}:
                    raise ValueError("invalid command")
                action, value = command["action"], command["value"]
                if action == "page":
                    if value not in SCREEN_STATES:
                        raise ValueError("invalid page")
                    transition(value)
                elif action == "tap":
                    if (not isinstance(value, list) or len(value) != 2
                            or any(type(v) is not int for v in value)
                            or not 0 <= value[0] < WIDTH or not 0 <= value[1] < HEIGHT):
                        raise ValueError("invalid tap coordinates")
                    if state not in ("panel", "fans"):
                        raise ValueError("tap unavailable on console/off")
                    handle_action("tap", tuple(value))
                elif action == "brightness":
                    if type(value) is not int or not 2 <= value <= 100:
                        raise ValueError("invalid brightness")
                    nonlocal brightness
                    brightness = max(minimum, min(maximum, round(maximum * value / 100)))
                    if state != "off":
                        (backlight / "brightness").write_text(str(brightness))
                    if state in ("panel", "fans"):
                        refresh(use_cache=True)
                elif action == "rgb":
                    if (not isinstance(value, list) or len(value) != 4
                            or any(type(v) is not int for v in value)):
                        raise ValueError("invalid RGB request")
                    current = RGB.read_state()
                    if value[0] in (1, 3) or tuple(value[1:]) != current[1:]:
                        if not Path("/var/lib/r1-web/rgb-verified").exists():
                            raise ValueError("color and effect modes await visual verification")
                    actual = RGB.set_state(*value)
                    if last_data is not None:
                        last_data["rgb"] = actual[0] != 0
                    if state == "fans":
                        refresh(use_cache=True)
                    return {"ok": True, "state": state, "rgb": actual}
                elif action == "rgb_state":
                    if value is not None:
                        raise ValueError("invalid RGB status request")
                    return {"ok": True, "state": state, "rgb": RGB.read_state()}
                else:
                    raise ValueError("unknown command")
                return {"ok": True, "state": state}

            try:
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))
                switch_vt(2)
                set_tty2_cursor(False)
                time.sleep(0.35)
                refresh()
                next_refresh = time.monotonic() + 15
                while not STOP:
                    publish_state(state, round(100 * brightness / maximum))
                    timeout = min(1, max(0, next_refresh - time.monotonic()))
                    readable, _, _ = select.select([touch_fd, listener], [], [], timeout)
                    if listener in readable:
                        connection, _ = listener.accept()
                        with connection:
                            connection.settimeout(0.2)
                            try:
                                payload = connection.recv(MAX_COMMAND + 1)
                                if len(payload) > MAX_COMMAND:
                                    raise ValueError("command too large")
                                response = handle_command(json.loads(payload.decode("utf-8")))
                            except (OSError, ValueError, TypeError, RuntimeError) as error:
                                response = {"ok": False, "error": str(error)[:120]}
                            connection.sendall(json.dumps(response).encode("utf-8"))
                    if touch_fd in readable:
                        try:
                            events = gesture.feed(os.read(touch_fd, 24 * 64))
                        except BlockingIOError:
                            events = []
                        for action, value in events:
                            try:
                                handle_action(action, value)
                            except (OSError, RuntimeError, ValueError) as error:
                                print(f"gesture={action} error={error}", flush=True)
                    if state in ("panel", "fans") and time.monotonic() >= next_refresh:
                        status = control_status() if state == "fans" else None
                        fast = bool(status and status["live"]
                                    and (status["desired"] != status["mode"]
                                         or status["mode"] == "full"))
                        _, sequence = collector.latest()
                        refresh(use_cache=fast and sequence == last_sequence)
                        next_refresh = time.monotonic() + (1 if fast else 15)
                    elif state in ("panel", "fans"):
                        _, sequence = collector.latest()
                        if sequence != last_sequence:
                            refresh()
            finally:
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))
                set_tty2_cursor(True)
                switch_vt(1)
    finally:
        collector.stop()
        listener.close()
        os.close(fb_fd)
        os.close(touch_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", help="Write a PNG; do not touch the screen")
    parser.add_argument("--view", choices=("panel", "fans"), default="panel",
                        help="Dashboard page to render in preview mode")
    parser.add_argument("--initial-view", choices=("panel", "fans"), default="panel",
                        help="Dashboard page shown first in touch mode")
    parser.add_argument("--once", action="store_true", help="Draw once and exit")
    parser.add_argument("--touch", action="store_true", help="Enable tap/vertical-swipe trial")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    painter = Painter()
    try:
        if args.touch:
            run_touch(painter, initial_view=args.initial_view)
            return
        if args.preview:
            painter.draw(screen_snapshot(), view=args.view)
            painter.png(args.preview)
            return
        fd = os.open("/dev/fb0", os.O_RDWR)
        try:
            line_length = framebuffer_info(fd)
            with mmap.mmap(fd, line_length * HEIGHT, mmap.MAP_SHARED, mmap.PROT_WRITE) as framebuffer:
                def blit():
                    pixels, stride = painter.pixels()
                    for y in range(HEIGHT):
                        framebuffer[y * line_length:y * line_length + WIDTH * 4] = pixels[y * stride:y * stride + WIDTH * 4]

                while not STOP:
                    painter.draw(screen_snapshot())
                    blit()
                    if args.once:
                        break
                    for _ in range(15):
                        if STOP:
                            break
                        time.sleep(1)
                if STOP:
                    painter.end_screen()
                    blit()
        finally:
            os.close(fd)
    finally:
        painter.close()


if __name__ == "__main__":
    main()
