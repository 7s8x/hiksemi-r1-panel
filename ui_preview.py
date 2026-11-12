"""Loopback-only current-layout preview using synthetic data and no device API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import time

from r1_web_views import render_information, render_fans

ROOT = Path(__file__).resolve().parent


def preview_web_source():
    return (ROOT / "r1_web.html").read_text(encoding="utf-8")


def demo_data():
    return {
        "clock": time.strftime("%H:%M"), "uptime": 864000, "pve_version": "PROXMOX VE 9.1.1",
        "cpu_model": "Intel N100 · 4C / 4T", "cpu": 65, "cpu_usage": 28,
        "mem_total": 15, "mem_used": 10.4,
        "storage": [(("1TB", "NVME", "****"), 43), (("BAY 1  2TB", "SSD", "****"), 39),
                    (("BAY 2  1TB", "HDD", "****"), 38), (("BAY 3  4TB", "HDD", "****"), 39),
                    (("BAY 4  4TB", "HDD", "****"), 37)],
        "storage_models": ["DEMO NVMe", "DEMO SSD", "DEMO HDD", "DEMO HDD", "DEMO HDD"],
        "storage_usage": [6, 4, 77, 87, 40],
        "storage_capacity": [(60*10**9, 940*10**9, 6), (80*10**9, 1920*10**9, 4),
                             (770*10**9, 230*10**9, 77), (3480*10**9, 520*10**9, 87),
                             (1600*10**9, 2400*10**9, 40)],
        "vms": [("VM 100", "RUNNING"), ("VM 111", "RUNNING")],
        "fans": {"fan2_input": 1464, "fan3_input": 3994, "pwm2": 184, "pwm3": 184},
        "control": {"live": True, "mode": "high", "desired": "high"},
        "fan_basis": (65, 39, 4), "rgb": False,
    }


def demo_svg(page, theme):
    return (render_information if page == "panel" else render_fans)(demo_data(), theme=theme)


class Handler(BaseHTTPRequestHandler):
    timeout = 5
    def log_message(self, *args):
        pass
    def do_GET(self):
        parsed = urlsplit(self.path)
        theme = parse_qs(parsed.query).get("theme", ["mocha"])[0]
        if theme not in ("mocha", "latte"):
            self.send_error(400); return
        if parsed.path in ("/panel.svg", "/fans.svg"):
            payload = demo_svg(parsed.path[1:-4], theme); mime = "image/svg+xml"
        elif parsed.path == "/":
            payload = ("<!doctype html><meta charset=utf-8><title>R1 当前布局预览</title>"
                       "<style>body{background:#313244;color:#cdd6f4;font:16px sans-serif}a{color:#89dceb}"
                       ".pages{display:flex;gap:24px;flex-wrap:wrap}img{width:376px;height:960px}</style>"
                       "<h2>本地模拟预览</h2><p>演示数值 · 不连接 R1 · 不控制硬件</p>"
                       "<p><a href='/?theme=mocha'>Mocha</a> · <a href='/?theme=latte'>Latte</a></p>"
                       f"<div class=pages><img src='/panel.svg?theme={theme}' alt=信息页>"
                       f"<img src='/fans.svg?theme={theme}' alt=风扇页></div>").encode()
            mime = "text/html; charset=utf-8"
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(payload)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8766), Handler)
    print("R1 synthetic preview: http://127.0.0.1:8766/", flush=True)
    server.serve_forever()
