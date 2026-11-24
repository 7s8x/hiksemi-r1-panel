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


def load_color(percent, warn, high):
    if percent is not None and percent >= high:
        return "#f38ba8"
    if percent is not None and percent >= warn:
        return "#f4b46f"
    return "#74bffe"


def information_values(data: dict) -> dict:
    """Format display fields without building an image or reading hardware."""
    values = _shared(data)
    usage = _number(data.get("cpu_usage"))
    cpu_temp, cpu_unit = _temperature(data.get("cpu"))
    total = _number(data.get("mem_total"), 0, 10000)
    used = _number(data.get("mem_used"), 0, 10000)
    used_text = f"{used:.1f}" if used is not None else "--"
    total_text = f"{total:.0f}" if total is not None else "--"
    memory_digits = len(used_text) + len(total_text)
    values.update(CPU_MODEL=data.get("cpu_model") or "CPU",
                  CPU_ARC=round(364 * usage / 100) if usage is not None else 0,
                  CPU_ARC_COLOR=load_color(round(usage) if usage is not None else None, 60, 85),
                  CPU_USAGE=f"{round(usage)}%" if usage is not None else "--",
                  CPU_TEMP=cpu_temp, CPU_TEMP_UNIT=cpu_unit,
                  MEM_USED=used_text, MEM_TOTAL=total_text,
                  MEM_FONT=25 if memory_digits <= 6 else 21 if memory_digits <= 8 else 16)
    states = list(data.get("bay_states") or [])
    storage = list(data.get("storage") or [])
    percents = list(data.get("storage_usage") or [])
    capacities = list(data.get("storage_capacity") or [])
    for index in range(5):
        identity, temperature = storage[index] if index < len(storage) else (("--", "UNVERIFIED", "--"), None)
        raw_size, kind = str(identity[0]), str(identity[1])
        size = re.sub(r"^BAY [1-4]\s+", "", raw_size) if index else raw_size
        known = kind != "UNVERIFIED" and size != "--"
        title = "NVMe" if index == 0 else f"BAY {index}"
        detail = ("待核实" if not known else size if index == 0 else f"{size} {kind}")
        if index and index - 1 < len(states) and states[index - 1] == "empty":
            detail = "空盘位"
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
        detail = detail if len(detail) <= 16 else detail[:15].rstrip() + "…"
        detail_width = sum(0.7 if c.isascii() else 1 for c in detail)
        values.update({
            f"D{index}_TITLE": title,
            f"D{index}_DETAIL": detail,
            f"D{index}_DETAIL_FONT": min(30, int(165 / max(1, detail_width))),
            f"D{index}_TEMP": temp,
            f"D{index}_UNIT": unit,
            f"D{index}_BAR": round(290 * percent / 100) if percent is not None else 0,
            f"D{index}_BAR_COLOR": load_color(percent, 75, 90),
            f"D{index}_USED": used_text,
            f"D{index}_TOTAL": total_text,
        })

    return values



def fit_vm_label(name, tile_width):
    from unicodedata import east_asian_width, category
    budget = (tile_width - 12) // 7
    widths = [0 if category(c).startswith('M') else 2 if east_asian_width(c) in ('F', 'W') else 1
              for c in name]
    if sum(widths) <= budget:
        return name
    used = 0
    for i, width in enumerate(widths):
        if used + width >= budget:
            return name[:i].rstrip() + '…'
        used += width
    return name


def vm_values(data):
    vms = list(data.get("vms") or [])
    total = len(vms)
    unavailable = bool(total) and (data.get("freshness") or {}).get("groups", {}).get("vm") is False
    if unavailable:
        vms = [(name, "UNKNOWN") for name, _ in vms]
    running = sum(status == "RUNNING" for _, status in vms)
    badge = f"{'—' if unavailable else running} / {total}"
    width = 82 if len(badge) > 5 else 65
    summary = ""
    if total > 3:
        extra = vms[3:]
        summary = f"另有 {len(extra)} 台"
        if not unavailable:
            stopped = sum(status == "STOPPED" for _, status in extra)
            unknown = sum(status not in ("RUNNING", "STOPPED") for _, status in extra)
            if stopped:
                summary += f" · {stopped} 台已停止"
            if unknown:
                summary += f" · {unknown} 台未知"
    if unavailable:
        summary = f"{summary} · 数据暂不可用" if summary else "数据暂不可用"
    values = {
        "VM_BADGE_TEXT": badge,
        "VM_BADGE_COLOR": "#9ee8ce" if total and running == total else "#f4b46f" if total else "#9bb1be",
        "VM_BADGE_WIDTH": width, "VM_BADGE_X": 344 - width,
        "VM_BADGE_DOT_X": 357 - width, "VM_BADGE_TEXT_X": 370 - width,
        "VM_EMPTY_TEXT": "" if total else "未配置监控",
        "VM_SUMMARY": summary,
    }
    visible = min(total, 3)
    tile_width = (312 - 6 * (visible - 1)) // visible if visible else 0
    for index in range(3):
        shown = index < visible
        vmid, status = vms[index] if shown else ("", "UNKNOWN")
        x = 32 + index * (tile_width + 6) if shown else 32
        center = x + tile_width / 2
        values.update({f"VM{index}_{key}": value for key, value in {
            "X": x, "WIDTH": tile_width if shown else 0, "VISIBLE": int(shown),
            "CENTER": f"{center:g}", "DOT_X": f"{center - 26:g}", "STATUS_X": f"{center + 8:g}",
            "NAME": fit_vm_label(str(data.get("vm_display_names", {}).get(vmid, vmid)), tile_width) if shown else "",
            "STATUS": {"RUNNING": "运行中", "STOPPED": "已停止"}.get(status, "状态未知") if shown else "",
            "COLOR": "#9ee8ce" if status == "RUNNING" else "#f4b46f",
        }.items()})
    return values

def render_information(data: dict, *, web_layout: bool = True, theme: str | None = None) -> bytes:
    """Compatibility renderer, also used by the physical panel."""
    return theme_svg(_template("r1_web_information_web.svg", information_values(data)), theme)


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


def valid_rgb_state(state):
    """Only four validated bytes may enter the display's RGB projection."""
    if (isinstance(state, (list, tuple)) and len(state) == 4
            and all(type(v) is int and 0 <= v <= 255 for v in state)
            and state[0] in (0, 1, 2, 3)):
        return tuple(state)
    return None


def fan_values(data: dict) -> dict:
    """Format display fields without building an image or reading hardware."""
    values = _shared(data)
    values.update(vm_values(data))
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
        arc_color = "#f4b46f" if fault else load_color(percent, 70, 90)
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
            status = {"high": "标准", "low": "静音", "full": "全速"}.get(controller_state, "未知")
        prefix = f"F{channel}_"
        values.update({
            prefix + "RPM": str(round(rpm)) if rpm is not None else "--",
            prefix + "RPM_UNIT": "RPM" if rpm is not None else "",
            prefix + "RPM_FONT": 40 if rpm is not None and round(rpm) >= 10000 else 48,
            prefix + "RPM_COLOR": rpm_color,
            prefix + "OUTPUT": f"{percent}%" if percent is not None else "--",
            prefix + "OUTPUT_COLOR": output_color,
            prefix + "ARC": round(364 * percent / 100) if percent is not None else 0,
            prefix + "ARC_COLOR": arc_color,
            prefix + "STATUS": status,
            prefix + "STATUS_COLOR": "#9ee8ce" if status in ("标准", "静音", "全速") else "#f4b46f",
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
              if full_active else '')
    compact_status = status_text
    if full_active:
        back = {'high':'标准', 'low':'静音', 'auto':'标准'}.get(control.get('return_to'), '原档位')
        seconds = _number(control.get('remaining'), 0, 60)
        compact_status = f'全速 · {seconds}秒后返回{back}' if seconds is not None else f'全速 · 结束后返回{back}'
    button_detail = ''
    if full_active:
        button_detail = f'结束后返回{back}'
    fan_badge = {'high':'标准', 'low':'静音', 'full':'全速', 'offline':'离线', 'switching':'切换中'}.get(controller_state, '未知')
    if full_active and seconds is not None:
        fan_badge = f'全速 · {int(seconds)}s'
    badge_width = 104 if full_active and seconds is not None else 65
    badge_x = 344 - badge_width
    values.update(FAN_BADGE_WIDTH=badge_width, FAN_BADGE_X=badge_x,
                  FAN_BADGE_DOT_X=badge_x+13, FAN_BADGE_TEXT_X=badge_x+26,
                  FULL_BUTTON_DETAIL=button_detail, FULL_BUTTON_TEXT_Y=614 if full_active else 620, FAN_BADGE_TEXT=fan_badge,
                  FAN_BADGE_COLOR="#9ee8ce" if controller_state in ('high','low','full') else "#f4b46f",
                  CONTROL_CARD_STATUS=compact_status, CONTROL_STATUS=status_text, CONTROL_DETAIL=detail,
                  CONTROL_COLOR=control_color,
                  FULL_BUTTON_BG="#6bbfe5" if full_active else idle_bg,
                  FULL_BUTTON_FG=active_fg if full_active else idle_fg,
                  HIGH_BUTTON_BG="#6bbfe5" if high_active else idle_bg,
                  HIGH_BUTTON_FG=active_fg if high_active else idle_fg,
                  LOW_BUTTON_BG="#6bbfe5" if low_active else idle_bg,
                  LOW_BUTTON_FG=active_fg if low_active else idle_fg)
    rgb_state = valid_rgb_state(data.get("rgb_state"))
    mode = rgb_state[0] if rgb_state is not None else (0 if data.get("rgb") is False else None)
    rgb_status = {0: "灯光已关闭", 1: "常亮", 2: "彩虹", 3: "呼吸"}.get(mode, "灯光已开启" if data.get("rgb") is True else "灯光状态不可用")
    show_color = mode in (1, 3)
    swatch = ("#%02x%02x%02x" % tuple(rgb_state[1:])) if show_color else "none"
    values.update(RGB_BADGE_TEXT={0:"关闭",1:"常亮",2:"彩虹",3:"呼吸"}.get(mode,"未知"),
                  RGB_BADGE_COLOR="#9bb1be" if mode == 0 else "#9ee8ce" if mode is not None else "#f4b46f",
                  RGB_STATUS=rgb_status, RGB_COLOR="#9bb1be" if mode == 0 else "#edf6f8" if mode is not None else "#f4b46f",
                  RGB_SWATCH=swatch, RGB_SWATCH_OPACITY=int(show_color),
                  RGB_HEX_TEXT=swatch.upper() if show_color else "",
                  RGB_BREATHE_BG="#6bbfe5" if mode == 3 else idle_bg,
                  RGB_BREATHE_FG=active_fg if mode == 3 else idle_fg,
                  RGB_STEADY_BG="#6bbfe5" if mode == 1 else idle_bg,
                  RGB_STEADY_FG=active_fg if mode == 1 else idle_fg,
                  RGB_OFF_BUTTON_BG="#6bbfe5" if mode == 0 else idle_bg,
                  RGB_OFF_BUTTON_FG=active_fg if mode == 0 else idle_fg)
    basis = data.get("fan_basis")
    if not isinstance(basis, (tuple, list)) or len(basis) != 3:
        basis = (None, None, 0)
    cpu_temp, cpu_unit = _temperature(basis[0])
    drive_temp, drive_unit = _temperature(basis[1] if basis[2] == len(SITE.occupied_bays) else None)
    values.update(BASIS_CPU_TEMP=cpu_temp + cpu_unit,
                  BASIS_CPU_COLOR="#f5ad75" if cpu_unit else "#f4b46f",
                  BASIS_DRIVE_TEMP=drive_temp + drive_unit,
                  BASIS_DRIVE_COLOR="#75c4dc" if drive_unit else "#f4b46f")
    return values


def render_fans(data: dict, *, web_layout: bool = True, theme: str | None = None) -> bytes:
    """Compatibility renderer, also used by the physical panel."""
    return render_values("fans", fan_values(data), theme)


def render_values(view, values, theme):
    values = dict(values)
    swatch = values.get("RGB_SWATCH", "none")
    hex_text = values.get("RGB_HEX_TEXT", "")
    if view == "fans":
        values["RGB_SWATCH"] = "RGB_SWATCH_COLOR"
        values["RGB_HEX_TEXT"] = "RGB_LITERAL_TEXT"
    template = "r1_web_information_web.svg" if view == "panel" else "r1_web_fans_web.svg"
    svg = theme_svg(_template(template, values), theme)
    return svg.replace(b"RGB_SWATCH_COLOR", swatch.encode("ascii")).replace(b"RGB_LITERAL_TEXT", hex_text.encode("ascii"))
