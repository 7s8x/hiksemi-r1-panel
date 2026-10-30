"""Read-only Web layouts, rendered separately from the physical framebuffer."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess
from xml.sax.saxutils import escape

from r1_theme import theme_svg


ROOT = Path(__file__).resolve().parent
TOKEN = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def _template(name: str, values: dict[str, object]) -> bytes:
    source = (ROOT / name).read_text(encoding="utf-8")
    missing = set(TOKEN.findall(source)) - values.keys()
    if missing:
        raise ValueError(f"missing Web display fields: {sorted(missing)}")
    return TOKEN.sub(lambda match: escape(str(values[match.group(1)])), source).encode("utf-8")


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


def _number(value, low=0, high=100):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high:
        return value
    return None


def _temperature(value) -> tuple[str, str]:
    number = _number(value, -20, 150)
    return (str(round(number)), "°C") if number is not None else ("--", "")


def _uptime(seconds) -> str:
    value = _number(seconds, 0, 10**10)
    if value is None:
        return "运行时间 --"
    hours = int(value) // 3600
    return f"运行 {hours // 24} 天 {hours % 24} 小时"


def _shared(data: dict) -> dict[str, object]:
    return {
        "PVE_VERSION": data.get("pve_version") or "PROXMOX VE --",
        "UPTIME": _uptime(data.get("uptime")),
        "CLOCK": data.get("clock") if re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", str(data.get("clock"))) else "--:--",
    }


def _used_amount(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000_000_000:.2f}T" if size_bytes >= 10_000_000_000 else f"{size_bytes / 1_000_000_000:.0f}GB"


def _free_amount(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000_000_000:.2f}TB" if size_bytes >= 1_000_000_000_000 else f"{size_bytes / 1_000_000_000:.0f}GB"


def render_information(data: dict, *, web_layout: bool = False) -> bytes:
    values = _shared(data)
    usage = _number(data.get("cpu_usage"))
    cpu_temp, cpu_unit = _temperature(data.get("cpu"))
    total = _number(data.get("mem_total"), 0, 10000)
    used = _number(data.get("mem_used"), 0, 10000)
    values.update(CPU_MODEL=data.get("cpu_model") or "CPU",
                  CPU_ARC=round((364 if web_layout else 314) * usage / 100) if usage is not None else 0,
                  CPU_USAGE=f"{round(usage)}%" if usage is not None else "--",
                  CPU_TEMP=cpu_temp, CPU_TEMP_UNIT=cpu_unit,
                  MEM_USED=f"{used:.1f}" if used is not None else "--",
                  MEM_TOTAL=f"{total:.0f}" if total is not None else "--",
                  DRIVE_COUNT=5)
    storage = list(data.get("storage") or [])
    models = list(data.get("storage_models") or [])
    percents = list(data.get("storage_usage") or [])
    capacities = list(data.get("storage_capacity") or [])
    for index in range(5):
        identity, temperature = storage[index] if index < len(storage) else (("--", "UNVERIFIED", "--"), None)
        raw_size, kind = str(identity[0]), str(identity[1])
        size = re.sub(r"^BAY [1-4]\s+", "", raw_size) if index else raw_size
        known = kind != "UNVERIFIED" and size != "--"
        title = (f"NVMe · {size}" if index == 0 else
                 f"BAY {index} · {size} {kind}" if known else f"BAY {index} · 待核实")
        percent = _number(percents[index]) if index < len(percents) else None
        percent = round(percent) if percent is not None and known else None
        record = capacities[index] if index < len(capacities) else None
        capacity_valid = (known and isinstance(record, (tuple, list)) and len(record) == 3
                          and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in record)
                          and record[0] + record[1] > 0 and 0 <= record[2] <= 100
                          and percent is not None and abs(record[2] - percent) <= 1)
        if capacity_valid:
            percent = record[2]
            used_text = f"已用 {_used_amount(record[0])} · {percent}%"
            total_text = f"{_free_amount(record[1])} 可用"
        else:
            used_text = f"已用 -- · {percent}%" if percent is not None else "已用 --"
            total_text = "-- 可用" if known else "--"
        temp, unit = _temperature(temperature)
        model = str(models[index]) if index < len(models) else "--"
        values.update({
            f"D{index}_TITLE": title[:23],
            f"D{index}_MODEL": model[:24],
            f"D{index}_TEMP": temp,
            f"D{index}_UNIT": unit,
            f"D{index}_BAR": round(290 * percent / 100) if percent is not None else 0,
            f"D{index}_BAR_COLOR": "#f4b46f" if percent is not None and percent >= 90 else "#6bbfe5",
            f"D{index}_USED": used_text,
            f"D{index}_TOTAL": total_text,
        })
    vms = list(data.get("vms") or [])
    running = sum(status == "RUNNING" for _, status in vms[:2])
    values["VM_SUMMARY"] = f"{running} / {min(len(vms), 2)} 运行中"
    for index in range(2):
        vmid, status = vms[index] if index < len(vms) else (f"VM {index + 1}", "UNKNOWN")
        values[f"VM{index}_NAME"] = str(vmid)[:16]
        values[f"VM{index}_ID"] = ""
        values[f"VM{index}_STATUS"] = {"RUNNING": "运行中", "STOPPED": "已停止"}.get(status, "状态未知")
        values[f"VM{index}_COLOR"] = "#9ee8ce" if status == "RUNNING" else "#f4b46f"
    template = "r1_web_information_web.svg" if web_layout else "r1_web_information.svg"
    return theme_svg(_template(template, values))


def render_fans(data: dict, *, web_layout: bool = False) -> bytes:
    values = _shared(data)
    fans = data.get("fans") or {}
    control = data.get("control") or {}
    for channel in (2, 3):
        rpm = _number(fans.get(f"fan{channel}_input"), 0, 20000)
        pwm = _number(fans.get(f"pwm{channel}"), 0, 255)
        percent = round(pwm * 100 / 255) if pwm is not None else None
        stopped = rpm == 0 if rpm is not None else False
        fault = stopped or rpm is None
        arc_color = "#f4b46f" if fault else "#74bfff"
        rpm_color = "#f4b46f" if fault else "#75c4dc"
        output_color = "#f4b46f" if fault else "#edf6f8"
        if stopped:
            status = "停转"
        elif rpm is None:
            status = "读数不可用"
        elif not control.get("live"):
            status = "离线"
        elif control.get("desired") != control.get("mode"):
            status = "切换中"
        else:
            status = {"auto": "自动", "full": "全速"}.get(control.get("mode"), "未知")
        prefix = f"F{channel}_"
        values.update({
            prefix + "RPM": str(round(rpm)) if rpm is not None else "--",
            prefix + "RPM_UNIT": "RPM" if rpm is not None else "",
            prefix + "RPM_FONT": 29 if rpm is not None and rpm >= 10000 else 35,
            prefix + "RPM_COLOR": rpm_color,
            prefix + "OUTPUT": f"{percent}%" if percent is not None else "--",
            prefix + "OUTPUT_COLOR": output_color,
            prefix + "ARC": round(364 * percent / 100) if percent is not None else 0,
            prefix + "ARC_COLOR": arc_color,
            prefix + "STATUS": status,
            prefix + "STATUS_COLOR": "#9ee8ce" if status in ("自动", "全速") else "#f4b46f",
        })
    if not control.get("live"):
        control_status, control_mode, control_color = "温控服务离线", "--", "#f4b46f"
    elif control.get("desired") != control.get("mode"):
        control_status, control_mode, control_color = "正在切换模式", "--", "#f4b46f"
    elif control.get("mode") == "full":
        remaining = control.get("remaining")
        control_status, control_mode, control_color = "全速运行中", f"全速 · 剩余 {remaining} 秒" if remaining is not None else "全速", "#f4b46f"
    else:
        control_status, control_mode, control_color = "自动曲线运行中", "自动调节", "#71d4b7"
    full_active = control.get("live") and control.get("desired") == control.get("mode") == "full"
    auto_active = control.get("live") and control.get("desired") == control.get("mode") == "auto"
    active_bg, active_fg = "#71d4b7", "#0b121b"
    idle_bg, idle_fg = "#244354", "#edf6f8"
    values.update(CONTROL_STATUS=control_status, CONTROL_MODE=control_mode,
                  CONTROL_COLOR=control_color,
                  FULL_BUTTON_BG=active_bg if full_active else idle_bg,
                  FULL_BUTTON_FG=active_fg if full_active else idle_fg,
                  AUTO_BUTTON_BG=active_bg if auto_active else idle_bg,
                  AUTO_BUTTON_FG=active_fg if auto_active else idle_fg)
    rgb = data.get("rgb")
    values.update(RGB_STATUS="灯光已开启" if rgb is True else "灯光已关闭" if rgb is False else "灯光状态不可用",
                  RGB_COLOR="#71d4b7" if rgb is True else "#9bb1be" if rgb is False else "#f4b46f",
                  RGB_ON_BUTTON_BG=active_bg if rgb is True else idle_bg,
                  RGB_ON_BUTTON_FG=active_fg if rgb is True else idle_fg,
                  RGB_OFF_BUTTON_BG=active_bg if rgb is False else idle_bg,
                  RGB_OFF_BUTTON_FG=active_fg if rgb is False else idle_fg)
    basis = data.get("fan_basis")
    if not isinstance(basis, (tuple, list)) or len(basis) != 3:
        basis = (None, None, 0)
    cpu_temp, cpu_unit = _temperature(basis[0])
    drive_temp, drive_unit = _temperature(basis[1] if basis[2] == 4 else None)
    values.update(BASIS_CPU_TEMP=cpu_temp + cpu_unit,
                  BASIS_CPU_COLOR="#f5ad75" if cpu_unit else "#f4b46f",
                  BASIS_DRIVE_TEMP=drive_temp + drive_unit,
                  BASIS_DRIVE_COLOR="#75c4dc" if drive_unit else "#f4b46f")
    if web_layout and full_active:
        remaining = control.get("remaining")
        values["CONTROL_STATUS"] = (f"全速运行 · 剩余 {remaining} 秒"
                                    if remaining is not None else "全速运行")
    template = "r1_web_fans_web.svg" if web_layout else "r1_web_fans.svg"
    return theme_svg(_template(template, values))
