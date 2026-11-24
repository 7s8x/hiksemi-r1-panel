"""Loopback-only current-layout preview using synthetic data and no device API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import time
import json
from threading import Lock

from r1_web_dom import document_layout, project
from r1_web_views import information_values, fan_values

from r1_web_views import render_information, render_fans
from r1_fan_config import DEFAULT
from r1_vm_status import validate_labels

ROOT = Path(__file__).resolve().parent
VM_IDS = (100, 111, 200)
preview_selection = VM_IDS
preview_labels = {}
VM_NAMES = {"100": "Synology", "111": "OpenWrt", "200": "Home Assistant"}
preview_lock = Lock()


def preview_vm_settings():
    with preview_lock:
        return {"available": True, "vmids": VM_IDS, "selected": preview_selection,
                "names": VM_NAMES, "labels": preview_labels}


def preview_web_source():
    return (ROOT / "r1_web.html").read_text(encoding="utf-8")


def demo_data():
    selected = preview_vm_settings()["selected"]
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
        "vms": [(f"VM {vmid}", "RUNNING") for vmid in selected],
        "fans": {"fan2_input": 1464, "fan3_input": 3994, "pwm2": 184, "pwm3": 184},
        "control": {"live": True, "mode": "high", "desired": "high"},
        "fan_basis": (65, 39, 4), "rgb": False,
    }


def preview_data():
    data = demo_data()
    data["cpu_usage"] = 64
    data["fans"].update(pwm2=120, pwm3=245)
    data["storage_usage"][1:4] = [42, 78, 96]
    data["storage_capacity"][1:4] = [(840*10**9, 1160*10**9, 42),
                                     (780*10**9, 220*10**9, 78),
                                     (3840*10**9, 160*10**9, 96)]
    return data


def demo_svg(page, theme):
    return (render_information if page == "panel" else render_fans)(preview_data(), theme=theme)


class Handler(BaseHTTPRequestHandler):
    timeout = 5
    def log_message(self, *args):
        pass
    def _json(self, status, data):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status, payload, mime):
        self.send_response(status); self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(payload)

    def do_POST(self):
        global preview_selection, preview_labels
        if self.path != "/api/vm-monitor":
            return self._json(403, {"error": "模拟预览不支持设备或账户操作"})
        authority = f"127.0.0.1:{self.server.server_port}"
        if (self.headers.get("Host") != authority or self.headers.get("Origin") != f"http://{authority}"
                or self.headers.get("X-R1-CSRF") != "local-preview"):
            return self._json(403, {"error": "仅允许本机预览页保存"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            values = data["vmids"]
            labels = validate_labels(data.get("labels", {}))
            if (not isinstance(values, list) or len(values) > len(VM_IDS)
                    or not set(labels) <= VM_NAMES.keys()
                    or any(type(id) is not int or id not in VM_IDS for id in values)
                    or len(set(values)) != len(values)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "演示虚拟机选择无效"})
        with preview_lock:
            preview_selection = tuple(values)
            preview_labels = labels
        self._json(200, {"ok": True, **preview_vm_settings()})

    def do_GET(self):
        parsed = urlsplit(self.path)
        theme = parse_qs(parsed.query).get("theme", ["mocha"])[0]
        if theme not in ("mocha", "latte"):
            self.send_error(400); return
        if parsed.path in ("/panel.svg", "/fans.svg"):
            payload = demo_svg(parsed.path[1:-4], theme); mime = "image/svg+xml"
        elif parsed.path in ("/web", "/manage"):
            source = preview_web_source().replace("fetch('/api/theme',", "fetch('/api/theme?theme=" + theme + "',").replace("'/public/state.json'", "'/public/state.json?theme=" + theme + "'").replace("'/public/layout.json'", "'/public/layout.json?theme=" + theme + "'").replace("'/api/display-state'", "'/api/display-state?theme=" + theme + "'")
            if parsed.path == "/manage":
                source = source.replace("api('/api/status')", "api('/api/status?preview=1')")
                source = source.replace("实体屏实时镜像 · 风扇与灯光设置", "本机模拟 · 仅测试虚拟机监控 · 不连接 PVE")
            payload = source.encode()
            mime = "text/html; charset=utf-8"
        elif parsed.path == "/api/status" and parsed.query == "preview=1":
            return self._json(200, {"csrf": "local-preview", "panel": None,
                                    "fan": {"live": False}, "curves": DEFAULT,
                                    "rgb_color_enabled": False})
        elif parsed.path == "/api/vm-monitor":
            return self._json(200, preview_vm_settings())
        elif parsed.path == "/api/rgb":
            return self._json(200, {"rgb": [0, 0, 0, 0]})
        elif parsed.path in ("/public/state.json", "/public/layout.json", "/api/display-state", "/api/theme"):
            spec = document_layout(theme)
            sample = preview_data()
            if parsed.path == "/api/display-state":
                sample["vm_display_names"] = {
                    f"VM {key}": VM_NAMES[key]
                    for key, spec in preview_vm_settings()["labels"].items() if spec["mode"] == "pve"}
            data = ({'theme': theme} if parsed.path == '/api/theme' else spec
                    if parsed.path.endswith('layout.json') else
                    {'id': spec['id'], 'theme': theme, 'valid_for': 30,
                     'pages': {'panel': project('panel', information_values(sample), theme),
                               'fans': project('fans', fan_values(sample), theme)}})
            payload = json.dumps(data).encode(); mime = 'application/json'
        elif parsed.path == "/":
            payload = ("<!doctype html><meta charset=utf-8><title>R1 当前布局预览</title>"
                       "<style>body{background:#313244;color:#cdd6f4;font:16px sans-serif}a{color:#89dceb}"
                       ".pages{display:flex;gap:24px;flex-wrap:wrap}img{width:376px;height:960px}</style>"
                       "<h2>本地模拟预览</h2><p>演示数值 · 不连接 R1 · 不控制硬件</p>"
                       "<p><a href='/manage'>测试虚拟机监控设置</a></p>"
                       "<p><a href='/?theme=mocha'>Mocha</a> · <a href='/?theme=latte'>Latte</a></p>"
                       f"<div class=pages><img src='/panel.svg?theme={theme}' alt=信息页>"
                       f"<img src='/fans.svg?theme={theme}' alt=风扇页></div>").encode()
            mime = "text/html; charset=utf-8"
        else:
            self.send_error(404); return
        self._send(200, payload, mime)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8766), Handler)
    print("R1 synthetic preview: http://127.0.0.1:8766/", flush=True)
    server.serve_forever()
