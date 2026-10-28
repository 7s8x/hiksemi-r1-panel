"""Loopback-only, hardware-free preview of the existing R1 Web UI.

This server intentionally never imports the production Web backend or connects
to R1. All controls update disposable in-memory demo state only.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8766
BASELINE = {
    "version": 1,
    "cpu": [[55, 120], [65, 120], [70, 140], [80, 180], [90, 220], [95, 255]],
    "drive": [[35, 120], [40, 120], [45, 140], [50, 180], [55, 220], [60, 255]],
}


class PreviewState:
    page = "fans"
    variant = "original"
    brightness = 65
    fan_mode = "auto"
    full_until = 0.0
    rgb = [0, 255, 0, 0]
    curves = json.loads(json.dumps(BASELINE))


STATE = PreviewState()


def preview_web_source() -> str:
    """Serve the same percentage-based editor as production, with mock APIs."""
    return (ROOT / "r1_web.html").read_text(encoding="utf-8")


def overview_svg(clock: str = "09:28", variant: str = "original") -> bytes:
    """Original draft and three temperature-focused, hardware-free concepts."""
    bg, panel, muted, fg = "#09141d", "#132834", "#91a9b4", "#e7f2f6"
    cyan, green, amber = "#67b9d0", "#70d5b7", "#f7ad6d"
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="376" height="960" viewBox="0 0 376 960">',
        f'<rect width="376" height="960" fill="{bg}"/>',
        '<style>text{font-family:DejaVu Sans,Arial,sans-serif}</style>',
    ]

    def rect(x, y, width, height, color, radius=0):
        rounding = f' rx="{radius}"' if radius else ""
        parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}"{rounding} fill="{color}"/>')

    def label(x, y, value, size=14, color=fg, weight="normal"):
        parts.append(f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}">{escape(str(value))}</text>')

    disks = [
        ("2TB  NVME", 6, 43),
        ("4TB  HDD", 40, 34),
        ("4TB  HDD", 87, 36),
        ("2TB  SSD", 4, 37),
        ("1TB  HDD", 97, 32),
    ]

    def system_cards(offset=0):
        label(24, 777 + offset, "SYSTEM", 17, fg, "bold")
        rect(20, 795 + offset, 161, 119, panel, 6)
        rect(195, 795 + offset, 161, 119, panel, 6)
        label(34, 821 + offset, "VM STATUS", 12, muted)
        label(34, 852 + offset, "Synology", 15, green, "bold")
        label(154, 852 + offset, "ON", 12, green, "bold")
        label(34, 879 + offset, "ImmortalWrt", 15, green, "bold")
        label(154, 879 + offset, "ON", 12, green, "bold")
        label(209, 821 + offset, "MEMORY USED", 12, muted)
        label(209, 867 + offset, "8.1", 36, fg, "bold")
        label(277, 867 + offset, "/ 15 GiB", 13, muted)
        rect(209, 891 + offset, 133, 5, "#1f3844")
        rect(209, 891 + offset, 72, 5, cyan)

    label(24, 42, "HIKSEMI R1  /  PVE", 14, cyan, "bold")
    if variant == "original":
        label(24, 80, "HOST OVERVIEW", 27, fg, "bold")
        rect(24, 104, 9, 9, green)
        label(42, 115, "HOST ONLINE", 14, green)
        label(24, 174, clock, 43, fg, "bold")
        label(240, 143, "UPTIME", 12, muted)
        label(240, 171, "1D 16H", 24, cyan, "bold")
        rect(20, 194, 336, 142, panel, 6)
        rect(187, 212, 1, 88, "#29414f")
        label(40, 222, "CPU USAGE", 13, muted)
        label(207, 222, "CPU TEMP", 13, muted)
        label(38, 275, "28%", 49, fg, "bold")
        label(204, 275, "60", 49, fg, "bold")
        label(288, 274, "°C", 22, fg)
        rect(40, 295, 127, 6, "#1f3844")
        rect(40, 295, 36, 6, green)
        rect(207, 295, 129, 6, "#1f3844")
        rect(207, 295, 74, 6, amber)
        label(40, 324, "SIMULATED DATA / 15 SEC REFRESH", 11, muted)
        label(24, 367, "HARDWARE TEMPERATURE", 17, fg, "bold")
        for index, (identity, used, temperature) in enumerate(disks):
            y = 384 + index * 80
            rect(20, y, 336, 76, panel, 6)
            label(36, y + 30, identity, 18, fg, "bold")
            label(36, y + 57, "USED", 11, muted)
            label(78, y + 57, f"{used}%", 16, green, "bold")
            rect(36, y + 65, 146, 4, "#1f3844")
            rect(36, y + 65, round(146 * used / 100), 4, green)
            label(251, y + 50, temperature, 34, cyan, "bold")
            label(307, y + 49, "°C", 17, cyan, "bold")
    elif variant == "focus":
        label(24, 80, "THERMAL FOCUS", 27, fg, "bold")
        rect(24, 104, 9, 9, green)
        label(42, 115, "HOST ONLINE", 14, green)
        label(24, 174, clock, 43, fg, "bold")
        label(240, 143, "UPTIME", 12, muted)
        label(240, 171, "1D 16H", 20, cyan, "bold")
        rect(20, 194, 336, 164, panel, 6)
        rect(221, 214, 1, 113, "#29414f")
        label(40, 224, "CPU TEMP", 14, muted)
        label(238, 224, "CPU USAGE", 13, muted)
        label(38, 304, "60", 72, fg, "bold")
        label(141, 302, "°C", 24, fg)
        label(238, 297, "28%", 34, fg, "bold")
        rect(40, 323, 155, 6, "#1f3844")
        rect(40, 323, 91, 6, amber)
        rect(238, 323, 97, 6, "#1f3844")
        rect(238, 323, 27, 6, green)
        label(40, 347, "SIMULATED DATA / 15 SEC REFRESH", 11, muted)
        label(24, 390, "DRIVE TEMPERATURE", 17, fg, "bold")
        for index, (identity, used, temperature) in enumerate(disks):
            y = 408 + index * 69
            rect(20, y, 336, 61, panel, 6)
            label(36, y + 26, identity, 16, fg, "bold")
            label(36, y + 48, f"USED {used}%", 11, green)
            label(264, y + 39, f"{temperature}°C", 25, cyan, "bold")
    elif variant == "ranking":
        label(24, 82, "THERMAL RANKING", 25, fg, "bold")
        label(24, 111, "HIGHEST TEMPERATURE FIRST", 12, muted)
        label(24, 158, clock, 40, fg, "bold")
        label(235, 137, "UPTIME", 12, muted)
        label(235, 160, "1D 16H", 19, cyan, "bold")
        rect(20, 183, 336, 78, panel, 6)
        label(36, 214, "CPU PACKAGE", 14, fg, "bold")
        label(36, 240, "USAGE 28%", 12, muted)
        label(254, 235, "60°C", 30, amber, "bold")
        label(24, 291, "STORAGE / SORTED BY TEMPERATURE", 15, fg, "bold")
        sorted_disks = sorted(disks, key=lambda item: item[2], reverse=True)
        for index, (identity, used, temperature) in enumerate(sorted_disks):
            y = 309 + index * 89
            rect(20, y, 336, 81, panel, 6)
            label(36, y + 25, identity, 16, fg, "bold")
            label(36, y + 48, f"USED {used}%", 11, green)
            label(249, y + 47, f"{temperature}°C", 27, cyan, "bold")
            rect(36, y + 66, 303, 4, "#1f3844")
            rect(36, y + 66, round(303 * temperature / 80), 4, cyan)
    elif variant == "bars":
        label(24, 82, "THERMAL MAP", 27, fg, "bold")
        label(24, 115, clock, 34, fg, "bold")
        label(226, 112, "UPTIME  1D 16H", 13, cyan, "bold")
        label(24, 157, "TEMPERATURE / 0–80°C SCALE", 14, muted)
        readings = [("CPU PACKAGE", "USAGE 28%", 60, amber)] + [
            (identity, f"USED {used}%", temperature, cyan) for identity, used, temperature in disks
        ]
        for index, (identity, detail, temperature, color) in enumerate(readings):
            y = 177 + index * 96
            rect(20, y, 336, 88, panel, 6)
            label(36, y + 26, identity, 15, fg, "bold")
            label(36, y + 48, detail, 11, muted)
            label(266, y + 45, f"{temperature}°C", 25, color, "bold")
            rect(36, y + 65, 304, 8, "#1f3844")
            rect(36, y + 65, round(304 * temperature / 80), 8, color)
    else:
        raise ValueError("invalid preview variant")
    system_cards(31 if variant == "original" else 0)
    parts.append("</svg>")
    return "".join(parts).encode("utf-8")


def fan_svg(clock: str = "20:44", pwm: int = 120,
            rgb_on: bool = False, full_speed: bool = False) -> bytes:
    """Fan-page draft with the site-confirmed FAN 2/3 roles."""
    percent = round(pwm * 100 / 255)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="376" height="960" viewBox="0 0 376 960">',
        '<rect width="376" height="960" fill="#09141d"/>',
        '<style>text{font-family:DejaVu Sans,Arial,sans-serif}</style>',
        '<text x="24" y="42" fill="#67b9d0" font-size="14" font-weight="bold">HIKSEMI R1 / PVE</text>',
        '<text x="24" y="80" fill="#e7f2f6" font-size="27" font-weight="bold">FAN MONITOR</text>',
        '<rect x="24" y="105" width="9" height="9" fill="#70d5b7"/>',
        '<text x="42" y="116" fill="#70d5b7" font-size="14">IT8613E / LIVE</text>',
        f'<text x="24" y="174" fill="#e7f2f6" font-size="43" font-weight="bold">{escape(clock)}</text>',
        '<text x="240" y="143" fill="#91a9b4" font-size="12">UPTIME</text>',
        '<text x="240" y="171" fill="#67b9d0" font-size="24" font-weight="bold">1D 16H</text>',
    ]
    for index, (name, channel, rpm) in enumerate((
        ("SYS FAN", 2, 995), ("CPU FAN", 3, 2836)
    )):
        y = 194 + index * 210
        parts += [
            f'<rect x="20" y="{y}" width="336" height="197" rx="6" fill="#132834"/>',
            f'<text x="40" y="{y+33}" fill="#e7f2f6" font-size="19" font-weight="bold">{name}</text>',
            f'<text x="336" y="{y+33}" text-anchor="end" fill="#91a9b4" font-size="12">FAN {channel}</text>',
            f'<text x="40" y="{y+108}" fill="#70d5b7" font-size="66" font-weight="bold">{rpm}</text>',
            f'<text x="336" y="{y+105}" text-anchor="end" fill="#91a9b4" font-size="17">RPM</text>',
            f'<rect x="40" y="{y+126}" width="296" height="2" fill="#29414f"/>',
            f'<text x="40" y="{y+160}" fill="#e7f2f6" font-size="18">OUTPUT {percent}%</text>',
            f'<text x="40" y="{y+183}" fill="#91a9b4" font-size="12">SW AUTO / MANUAL PWM</text>',
        ]
    parts += [
        '<rect x="20" y="635" width="336" height="127" rx="6" fill="#132834"/>',
        '<text x="40" y="663" fill="#67b9d0" font-size="18" font-weight="bold">FAN CONTROL</text>',
        f'<text x="40" y="690" fill="{"#f7ad6d" if full_speed else "#70d5b7"}" font-size="15" font-weight="bold">{"FULL SPEED ACTIVE" if full_speed else "AUTO CURVE ACTIVE"}</text>',
        f'<rect x="30" y="706" width="150" height="44" rx="4" fill="{"#f7ad6d" if full_speed else "#203d4d"}"/>',
        f'<rect x="196" y="706" width="150" height="44" rx="4" fill="{"#203d4d" if full_speed else "#70d5b7"}"/>',
        f'<text x="43" y="734" fill="{"#09141d" if full_speed else "#e7f2f6"}" font-size="14" font-weight="bold">FULL SPEED</text>',
        f'<text x="211" y="734" fill="{"#e7f2f6" if full_speed else "#09141d"}" font-size="14" font-weight="bold">AUTO CURVE</text>',
        '<text x="24" y="783" fill="#e7f2f6" font-size="17" font-weight="bold">THERMAL / LIGHT</text>',
        '<text x="24" y="807" fill="#91a9b4" font-size="13">CPU 53°C   MAX DRIVE 39°C</text>',
        '<rect x="20" y="825" width="336" height="127" rx="6" fill="#132834"/>',
        '<text x="40" y="853" fill="#67b9d0" font-size="18" font-weight="bold">R1 RGB LIGHT</text>',
        f'<text x="40" y="880" fill="{"#70d5b7" if rgb_on else "#91a9b4"}" font-size="15" font-weight="bold">{"LIGHT ON" if rgb_on else "LIGHT OFF"}</text>',
        f'<rect x="30" y="896" width="150" height="44" rx="4" fill="{"#70d5b7" if rgb_on else "#203d4d"}"/>',
        f'<rect x="196" y="896" width="150" height="44" rx="4" fill="{"#203d4d" if rgb_on else "#70d5b7"}"/>',
        f'<text x="43" y="924" fill="{"#09141d" if rgb_on else "#e7f2f6"}" font-size="14" font-weight="bold">TURN ON</text>',
        f'<text x="211" y="924" fill="{"#e7f2f6" if rgb_on else "#09141d"}" font-size="14" font-weight="bold">TURN OFF</text>',
        '</svg>',
    ]
    return "".join(parts).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def _send(self, status: int, payload: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: dict):
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
        if self.headers.get("Host") not in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
            self._json(403, {"error": "loopback preview only"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            self._json(403, {"error": "cross-origin request denied"})
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        logo_names = {"r1-mark", "fan", "thermal", "panel"}
        r1_logo_names = {"neon", "flat", "light", "mono"}
        if path == "/logo-options/r1/":
            self._send(200, (ROOT / "logo-options" / "r1" / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path.startswith("/logo-options/r1/") and path.endswith(".svg") and path.rsplit("/", 1)[-1][:-4] in r1_logo_names:
            name = path.rsplit("/", 1)[-1]
            self._send(200, (ROOT / "logo-options" / "r1" / name).read_bytes(), "image/svg+xml")
        elif path == "/logo-options/":
            self._send(200, (ROOT / "logo-options" / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path.startswith("/logo-options/") and path.endswith(".svg") and path.rsplit("/", 1)[-1][:-4] in logo_names:
            name = path.rsplit("/", 1)[-1]
            self._send(200, (ROOT / "logo-options" / name).read_bytes(), "image/svg+xml")
        elif path == "/favicon.svg":
            self._send(200, (ROOT / "r1_web_favicon.svg").read_bytes(), "image/svg+xml")
        elif path == "/public-demo/":
            source = preview_web_source().replace(
                "<body>",
                '<body><div class="demo-notice">本地免登录看板模拟 · 非 R1 实时数据</div>'
                '<style>.demo-notice{padding:10px 18px;background:#9b5128;color:white;'
                'text-align:center;font-weight:700}</style>',
                1,
            )
            source = source.replace(
                "api('/api/status').then(data=>{csrf=data.csrf;$('public').classList.add('hide');$('login').classList.add('hide');$('app').classList.remove('hide');refresh()}).catch(()=>{});",
                "",
                1,
            )
            self._send(200, source.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/":
            source = preview_web_source()
            notice = (
                '<div class="demo-notice">本地模拟预览 · 所有读数与按钮均为演示，'
                '不连接 R1、不控制风扇或灯光</div>'
                '<div class="variant-chooser" aria-label="信息页设计方案">'
                '<span>信息页方案对比</span>'
                '<button type="button" data-variant="original" aria-pressed="true">原方案</button>'
                '<button type="button" data-variant="focus" aria-pressed="false">温度主视觉</button>'
                '<button type="button" data-variant="ranking" aria-pressed="false">高温优先</button>'
                '<button type="button" data-variant="bars" aria-pressed="false">温度对照</button>'
                '</div>'
                '<style>.demo-notice{position:sticky;top:0;z-index:20;padding:10px 18px;'
                'background:#9b5128;color:white;text-align:center;font-weight:700}'
                '.variant-chooser{display:flex;align-items:center;justify-content:center;'
                'gap:8px;flex-wrap:wrap;padding:10px;background:#132834;color:#e7f2f6}'
                '.variant-chooser span{margin-right:8px;font-weight:700}'
                '.variant-chooser button[aria-pressed="true"]{background:#167e8d;border-color:#36afbb}'
                '#showPassword,#logout{display:none!important}</style>'
                '<script>document.addEventListener("click",async function(event){'
                'const button=event.target.closest(".variant-chooser button[data-variant]");'
                'if(!button)return;const response=await fetch("/api/variant",{method:"POST",'
                'headers:{"Content-Type":"application/json"},'
                'body:JSON.stringify({variant:button.dataset.variant})});'
                'if(!response.ok)return;document.querySelectorAll(".variant-chooser button")'
                '.forEach(item=>item.setAttribute("aria-pressed",String(item===button)));'
                'document.querySelector("#pageButtons button[data-page=panel]").click();'
                '});</script>'
            )
            source = source.replace("<body>", "<body>" + notice, 1)
            source = source.replace("实体屏实时镜像 · 风扇与灯光设置", "模拟前屏 · 信息页布局草稿", 1)
            source = source.replace("<h2>实体屏</h2>", "<h2>模拟前屏</h2>", 1)
            self._send(200, source.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/public/panel.png":
            image = overview_svg(time.strftime("%H:%M"), "original")
            for secret in (b"DEMO-NVME-01", b"DEMO-HDD-01", b"DEMO-HDD-02",
                           b"DEMO-SSD-01", b"DEMO-HDD-03"):
                image = image.replace(secret, b"****")
            image = image.replace(b"Synology", b"VM 100").replace(b"ImmortalWrt", b"VM 111")
            self._send(200, image, "image/svg+xml")
        elif path == "/public/fans.png":
            self._send(200, fan_svg(time.strftime("%H:%M"), 120, False, False), "image/svg+xml")
        elif path == "/api/status":
            if self.headers.get("Referer", "").endswith("/public-demo/"):
                self._json(401, {"error": "login required"})
                return
            remaining = max(0, round(STATE.full_until - time.monotonic()))
            if not remaining:
                STATE.fan_mode = "auto"
            self._json(200, {
                "panel": {"state": STATE.page, "brightness": STATE.brightness},
                "fan": {"live": True, "mode": STATE.fan_mode,
                        "pwm": 255 if remaining else 120, "remaining": remaining},
                "curves": STATE.curves, "baseline": BASELINE,
                "curve_id": "DEMO", "curve_error": None,
                "rgb_color_enabled": True, "csrf": "local-preview-only",
            })
        elif path == "/api/rgb":
            self._json(200, {"ok": True, "rgb": STATE.rgb})
        elif path == "/api/frame.png":
            if STATE.page == "off":
                image = b'<svg xmlns="http://www.w3.org/2000/svg" width="376" height="960"><rect width="376" height="960" fill="black"/></svg>'
                self._send(200, image, "image/svg+xml")
            elif STATE.page == "panel":
                self._send(200, overview_svg(time.strftime("%H:%M"), STATE.variant), "image/svg+xml")
            else:
                full_speed = STATE.full_until > time.monotonic()
                pwm = 255 if full_speed else 120
                self._send(200, fan_svg(time.strftime("%H:%M"), pwm,
                                        STATE.rgb[0] != 0, full_speed), "image/svg+xml")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("invalid request length")
            body = json.loads(self.rfile.read(length))
            if path == "/api/panel":
                if body.get("action") == "page" and body.get("value") in ("panel", "fans", "off"):
                    STATE.page = body["value"]
                elif body.get("action") == "brightness" and type(body.get("value")) is int and 2 <= body["value"] <= 100:
                    STATE.brightness = body["value"]
                elif body.get("action") == "tap" and STATE.page != "off":
                    point = body.get("value")
                    if (not isinstance(point, list) or len(point) != 2
                            or any(type(value) is not int for value in point)
                            or not 0 <= point[0] < 376 or not 0 <= point[1] < 960):
                        raise ValueError("invalid demo tap coordinates")
                    x, y = point
                    if STATE.page == "fans":
                        if 30 <= x <= 180 and 896 <= y <= 940:
                            STATE.rgb[0] = 1
                        elif 196 <= x <= 346 and 896 <= y <= 940:
                            STATE.rgb[0] = 0
                        elif 30 <= x <= 180 and 706 <= y <= 750:
                            STATE.fan_mode = "full"
                            STATE.full_until = time.monotonic() + 60
                        elif 196 <= x <= 346 and 706 <= y <= 750:
                            STATE.fan_mode = "auto"
                            STATE.full_until = 0.0
                else:
                    raise ValueError("invalid demo panel action")
                self._json(200, {"ok": True, "state": STATE.page})
            elif path == "/api/variant" and body.get("variant") in ("original", "focus", "ranking", "bars"):
                STATE.variant = body["variant"]
                self._json(200, {"ok": True, "variant": STATE.variant})
            elif path == "/api/fan/mode" and body.get("mode") in ("auto", "full"):
                STATE.fan_mode = body["mode"]
                STATE.full_until = time.monotonic() + 60 if body["mode"] == "full" else 0.0
                self._json(200, {"ok": True})
            elif path == "/api/fan/curves" and isinstance(body, dict):
                # Preview edits are deliberately not persisted or applied to hardware.
                if not all(isinstance(body.get(key), list) for key in ("cpu", "drive")):
                    raise ValueError("invalid demo curves")
                STATE.curves = {"version": 1, "cpu": body["cpu"], "drive": body["drive"]}
                self._json(200, {"ok": True, "curves": STATE.curves, "curve_id": "DEMO"})
            elif path == "/api/rgb" and isinstance(body, dict):
                values = [body.get(key) for key in ("mode", "red", "green", "blue")]
                if any(type(value) is not int or not 0 <= value <= 255 for value in values) or values[0] > 3:
                    raise ValueError("invalid demo RGB value")
                STATE.rgb = values
                self._json(200, {"ok": True, "rgb": STATE.rgb})
            else:
                self._json(404, {"error": "not available in preview"})
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"R1 UI preview: http://{HOST}:{PORT}/ (mock only)", flush=True)
    server.serve_forever()
