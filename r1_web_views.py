"""Read-only Web layouts, rendered separately from the physical framebuffer."""

from __future__ import annotations

import math
from pathlib import Path
import re
from xml.sax.saxutils import escape

from r1_theme import theme_svg
from r1_site_config import SITE
# Explicit compatibility exports; callers may use these module attributes.
from r1_host_sampling import (
    system_identity as system_identity,
    drive_models as drive_models,
)


ROOT = Path(__file__).resolve().parent
TOKEN = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def _template(name: str, values: dict[str, object]) -> bytes:
    source = (ROOT / name).read_text(encoding="utf-8")
    missing = set(TOKEN.findall(source)) - values.keys()
    if missing:
        raise ValueError(f"missing Web display fields: {sorted(missing)}")
    return TOKEN.sub(lambda match: escape(str(values[match.group(1)])), source).encode("utf-8")


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


def render_information(data: dict, *, web_layout: bool = True, theme: str | None = None) -> bytes:
    """Render the shared current layout; web_layout remains a compatibility keyword."""
    values = _shared(data)
    usage = _number(data.get("cpu_usage"))
    cpu_temp, cpu_unit = _temperature(data.get("cpu"))
    total = _number(data.get("mem_total"), 0, 10000)
    used = _number(data.get("mem_used"), 0, 10000)
    values.update(CPU_MODEL=data.get("cpu_model") or "CPU",
                  CPU_ARC=round(364 * usage / 100) if usage is not None else 0,
                  CPU_USAGE=f"{round(usage)}%" if usage is not None else "--",
                  CPU_TEMP=cpu_temp, CPU_TEMP_UNIT=cpu_unit,
                  MEM_USED=f"{used:.1f}" if used is not None else "--",
                  MEM_TOTAL=f"{total:.0f}" if total is not None else "--")
    states = list(data.get("bay_states") or [])
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
        if index and index - 1 < len(states) and states[index - 1] == "empty":
            title = f"BAY {index} · 空盘位"
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
        values[f"VM{index}_STATUS"] = {"RUNNING": "运行中", "STOPPED": "已停止"}.get(status, "状态未知")
        values[f"VM{index}_COLOR"] = "#9ee8ce" if status == "RUNNING" else "#f4b46f"
    template = "r1_web_information_web.svg"
    return theme_svg(_template(template, values), theme)


_LEGACY_MODES = {"auto": "high"}


def _normal_mode(mode):
    return _LEGACY_MODES.get(mode, mode)


def _controller_state(control):
    """Normalize the retired ``auto`` status so old controller files keep rendering."""
    if not control.get('live'):
        return 'offline'
    desired = _normal_mode(control.get('desired'))
    mode = _normal_mode(control.get('mode'))
    if desired != mode:
        return 'switching'
    return mode if mode in ('high', 'low', 'full') else 'unknown'


def render_fans(data: dict, *, web_layout: bool = True, theme: str | None = None) -> bytes:
    """Render the shared current layout; web_layout remains a compatibility keyword."""
    values = _shared(data)
    fans = data.get("fans") or {}
    control = data.get("control") or {}
    controller_state = _controller_state(control)
    full_active = controller_state == "full"
    high_active = controller_state == "high"
    low_active = controller_state == "low"
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
        elif controller_state == "offline":
            status = "离线"
        elif controller_state == "switching":
            status = "切换中"
        else:
            status = {"high": "高速", "low": "低速", "full": "全速"}.get(controller_state, "未知")
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
            prefix + "STATUS_COLOR": "#9ee8ce" if status in ("高速", "低速", "全速") else "#f4b46f",
        })
    if controller_state == 'offline':
        status_text, control_color = '温控服务离线', '#f4b46f'
    elif controller_state == 'switching':
        status_text, control_color = '正在切换模式', '#f4b46f'
    elif full_active:
        remaining = control.get('remaining')
        status_text = f'全速散热 · 剩余 {remaining} 秒' if remaining is not None else '全速散热'
        control_color = '#f4b46f'
    elif high_active:
        status_text, control_color = '标准模式', '#6bbfe5'
    elif low_active:
        status_text, control_color = '静音模式', '#6bbfe5'
    else:
        status_text, control_color = '曲线状态未知', '#f4b46f'
    active_bg, active_fg = "#71d4b7", "#0b121b"
    idle_bg, idle_fg = "#244354", "#edf6f8"
    detail = ('结束后返回' + {'high': '标准模式', 'low': '静音模式', 'auto': '标准模式'}.get(control.get('return_to'), '原有模式')
              if full_active else '根据温度自动调节风量' if high_active else
              '优先降低噪音 · 高温自动提速' if low_active else '请核对温控服务状态')
    values.update(CONTROL_STATUS=status_text, CONTROL_DETAIL=detail,
                  CONTROL_COLOR=control_color,
                  FULL_BUTTON_BG="#6bbfe5" if full_active else idle_bg,
                  FULL_BUTTON_FG=active_fg if full_active else idle_fg,
                  HIGH_BUTTON_BG="#6bbfe5" if high_active else idle_bg,
                  HIGH_BUTTON_FG=active_fg if high_active else idle_fg,
                  LOW_BUTTON_BG="#6bbfe5" if low_active else idle_bg,
                  LOW_BUTTON_FG=active_fg if low_active else idle_fg)
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
    drive_temp, drive_unit = _temperature(basis[1] if basis[2] == len(SITE.occupied_bays) else None)
    values.update(BASIS_CPU_TEMP=cpu_temp + cpu_unit,
                  BASIS_CPU_COLOR="#f5ad75" if cpu_unit else "#f4b46f",
                  BASIS_DRIVE_TEMP=drive_temp + drive_unit,
                  BASIS_DRIVE_COLOR="#75c4dc" if drive_unit else "#f4b46f")
    template = "r1_web_fans_web.svg"
    return theme_svg(_template(template, values), theme)
