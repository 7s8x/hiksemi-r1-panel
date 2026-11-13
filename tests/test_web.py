"""Offline checks for HTTPS UI authentication and framebuffer encoder."""

import sys
import json
import ipaddress
import time
from http import cookies
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
import threading
import unittest
from xml.etree import ElementTree
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_web import (FAILURES, Handler, _sanitize_access_path, _web_log, SESSIONS, change_admin_password, client_allowed,
                    collect_public_frames, hash_password, public_snapshot, rgb_png,
                    verify_password)  # noqa: E402
from r1_panel_repository import WIDTH, HEIGHT  # noqa: E402
from r1_web_views import render_fans, render_information  # noqa: E402
from ui_preview import preview_web_source  # noqa: E402


class WebTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, target in (("r1_web.INITIAL_FILE", root / "initial-password"),
                             ("r1_web.WEB_LOG_FILE", root / "access.log"),
                             ("r1_theme.THEME_FILE", root / "theme.json")):
            patched = patch(name, target)
            patched.start()
            self.addCleanup(patched.stop)

    def test_public_snapshot_masks_serials_and_vm_names(self):
        source = {"storage": [(("2TB", "NVME", "SECRET-NVME"), 42),
                              (("4TB", "HDD", "SECRET-DISK"), 35)],
                  "storage_usage": [6, 87],
                  "storage_capacity": [(60_000_000_000, 940_000_000_000, 6), None],
                  "fan_basis": (65, 39, 4), "vms": [("VM 100", "RUNNING"), ("VM 111", "RUNNING")],
                  "ip": "PRIVATE", "generation": "SECRET"}
        with (patch("r1_web.read_snapshot", return_value=source),
              patch("r1_web.panel_repo.discover_drives", side_effect=AssertionError("duplicate collection")),
              patch("r1_web.control_status", return_value={"live": False}),
              patch("r1_web.request", return_value={"ok": True, "rgb": [2, 255, 0, 0]})):
            data = public_snapshot()
        self.assertEqual([identity[2] for identity, _ in data["storage"]], ["****", "****"])
        self.assertEqual(data["storage_usage"], [6, 87])
        self.assertEqual(data["storage_capacity"][0], (60_000_000_000, 940_000_000_000, 6))
        self.assertEqual(data["fan_basis"], (65, 39, 4))
        self.assertEqual(data["vms"], [("VM 100", "RUNNING"), ("VM 111", "RUNNING")])
        self.assertNotIn("SECRET", json.dumps(data))
        self.assertNotIn("secret disk label", json.dumps(data))
        self.assertNotIn("ip", data)

    def test_public_frames_render_without_physical_framebuffer(self):
        class FakePainter:
            views = []

            def draw(self, data, view):
                self.views.append(view)

            def pixels(self):
                return b"pixels", 4

            def close(self):
                pass

        with (patch("r1_web.public_snapshot", return_value={"storage": [], "vms": []}),
              patch("r1_web.panel_repo.Painter", FakePainter),
              patch("r1_web.paint_svg") as rasterize,
              patch("r1_web.rgb_png", return_value=b"PNG"),
              patch("r1_web.capture_frame", side_effect=AssertionError("physical framebuffer used"))):
            result = collect_public_frames()
        self.assertEqual(FakePainter.views, [])
        self.assertEqual(rasterize.call_count, 0)
        self.assertNotIn('panel', result)
        self.assertNotIn('fans', result)
        self.assertIn(b"<svg", result["panel_svg"])
        self.assertIn(b"<svg", result["fans_svg"])
        self.assertIn(b"height=\"633\"", result["panel_svg"])
        self.assertNotIn("虚拟机状态".encode(), result["panel_svg"])

    def test_web_views_escape_models_and_keep_fan_ring_semantics(self):
        data = {
            "clock": "12:40", "uptime": 86400, "cpu_usage": 25, "cpu": 56,
            "storage": [(("1TB", "NVME", "****"), 42)],
            "storage_models": ["Model <script>alert(1)</script>"],
            "storage_usage": [6], "storage_capacity": [(60_000_000_000, 940_000_000_000, 6)],
            "vms": [("VM 100", "RUNNING")],
            "fans": {"fan2_input": 995, "pwm2": 120, "fan3_input": 2836, "pwm3": 120},
            "control": {"live": True, "desired": "auto", "mode": "auto"},
            "drives": [("BAY 1", 39)], "fan_basis": (65, 39, 4), "rgb": False,
        }
        panel = render_information(data).decode("utf-8")
        web_panel = render_information(data, web_layout=True).decode("utf-8")
        fans = render_fans(data).decode("utf-8")
        self.assertEqual(panel, web_panel)
        self.assertEqual(render_information(data), render_information(data, web_layout=False))
        self.assertEqual(fans, render_fans(data, web_layout=True).decode("utf-8"))
        self.assertEqual(render_fans(data), render_fans(data, web_layout=False))
        self.assertIn("Model &lt;script&gt;", panel)
        self.assertNotIn("<script>", panel)
        self.assertIn("已用 0.06T · 6%", panel)
        self.assertIn("940GB 可用", panel)
        self.assertNotIn("1TB · 940GB 可用", panel)
        data["storage"].append((("2TB", "SSD", "****"), 39))
        self.assertIn("BAY 1 · 2TB SSD", render_information(data).decode("utf-8"))
        self.assertIn("VM 100", panel)
        self.assertIn("VM 100", web_panel)
        self.assertIn("运行中", web_panel)
        self.assertNotIn("虚拟机状态", web_panel)
        self.assertIn('height="197"', web_panel)
        self.assertIn('height="633"', web_panel)
        self.assertIn('>系统</text>', web_panel)
        self.assertIn('>存储</text>', web_panel)
        self.assertNotIn('>存储硬盘', web_panel)
        self.assertIn('cx="94" cy="209" r="58"', web_panel)
        self.assertIn('x="329" y="251"', web_panel)
        self.assertIn('x="329" y="269"', web_panel)
        self.assertIn('font-size="39" font-weight="700" dominant-baseline="middle" text-anchor="end">56', web_panel)
        self.assertNotIn("Synology", panel)
        self.assertIn("995", fans)
        self.assertIn("47%", fans)
        self.assertNotIn("环形进度按 PWM 输出比例绘制", fans)
        for label in ("全速", "标准", "静音"):
            self.assertIn(label, fans)
        self.assertIn("标准模式", fans)
        self.assertIn("呼吸", fans)
        self.assertIn("常亮", fans)
        self.assertIn("灯光已关闭", fans)
        self.assertNotIn("温控依据", fans)
        self.assertIn("硬盘最高温度", fans)
        self.assertLess(fans.index("风扇控制"), fans.index("硬盘最高温度"))
        self.assertLess(fans.index("风扇控制"), fans.index("灯光控制"))
        self.assertIn("65°C", fans)
        self.assertIn("39°C", fans)
        self.assertNotIn("温度与灯光", fans)
        self.assertNotIn("{{", panel + fans)
        fan_text = ElementTree.fromstring(fans).findall(".//{http://www.w3.org/2000/svg}text")
        self.assertTrue(any(node.attrib.get("x") == "94" and node.text == "47%" for node in fan_text))
        self.assertTrue(any(node.attrib.get("x") == "329" and node.text == "995" for node in fan_text))
        self.assertTrue(any(node.attrib.get("x") == "329" and node.text == "RPM" for node in fan_text))
        self.assertTrue(any(node.attrib.get("x") == "329" and node.text == "标准" for node in fan_text))
        self.assertNotIn("实际转速", fans)
        self.assertNotIn("风扇状态", fans)
        web_fans = render_fans(data, web_layout=True).decode("utf-8")
        self.assertNotIn("控制模式", web_fans)
        self.assertIn("机身 RGB", web_fans)
        self.assertNotIn("独立温控服务", web_fans)
        self.assertNotIn("根据温度自动调节风量", web_fans)
        data["control"] = {"live": True, "desired": "full", "mode": "full", "remaining": 45}
        self.assertIn("全速散热 · 剩余 45 秒", render_fans(data, web_layout=True).decode("utf-8"))
        self.assertIn(">全速</text>", render_fans(data).decode("utf-8"))
        data["fan_basis"] = (65, 39, 3)
        self.assertNotIn("39°C", render_fans(data).decode("utf-8"))

    def test_public_images_require_no_session_but_controls_still_do(self):
        class FakeCollector:
            def latest(self):
                return {"panel": b"panel PNG", "fans": b"fans PNG",
                        "panel_svg": b"<svg>panel</svg>", "fans_svg": b"<svg>fans</svg>",
                        "updated": time.time()}, 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch("r1_web.PUBLIC_COLLECTOR", FakeCollector()), patch("r1_web.PUBLIC_PNG.get", side_effect=lambda view, source: view.encode() + b" PNG"), patch.dict(SESSIONS, {}, clear=True):
                for name, expected in (("panel", b"panel PNG"), ("fans", b"fans PNG")):
                    with urlopen(base + f"/public/{name}.png", timeout=2) as response:
                        self.assertEqual(response.headers.get_content_type(), "image/png")
                        self.assertEqual(response.read(), expected)
                for name in ("panel", "fans"):
                    with urlopen(base + f"/public/{name}.svg", timeout=2) as response:
                        self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
                        self.assertEqual(response.read(), f"<svg>{name}</svg>".encode())
                with self.assertRaises(HTTPError) as denied:
                    urlopen(base + "/api/frame.png", timeout=2)
                self.assertEqual(denied.exception.code, 401)
                with self.assertRaises(HTTPError) as denied:
                    urlopen(base + "/api/status", timeout=2)
                self.assertEqual(denied.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_theme_is_publicly_readable_but_admin_only_writable(self):
        source = (Path(__file__).resolve().parents[1] / "r1_web.html").read_text(encoding="utf-8")
        self.assertIn('<option value="mocha">Catppuccin Mocha</option>', source)
        self.assertNotIn('<option value="current">', source)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def submit(theme, cookie=None, csrf=None):
            headers = {"Content-Type": "application/json",
                       "Origin": f"http://127.0.0.1:{server.server_port}"}
            if cookie:
                headers["Cookie"] = cookie
            if csrf:
                headers["X-R1-CSRF"] = csrf
            request = Request(base + "/api/theme", data=json.dumps({"theme": theme}).encode(), headers=headers)
            try:
                response = urlopen(request, timeout=2)
            except HTTPError as error:
                response = error
            with response:
                return response.status

        try:
            with patch.dict(SESSIONS, {"test": (time.time() + 60, "csrf")}, clear=True), \
                    patch("r1_web.load_theme", return_value="mocha"), \
                    patch("r1_web.save_theme") as save:
                with urlopen(base + "/api/theme", timeout=2) as response:
                    theme_data = json.loads(response.read())
                    self.assertEqual(theme_data["theme"], "mocha")
                    self.assertEqual(theme_data["choices"], ["mocha", "latte"])
                self.assertEqual(submit("mocha"), 401)
                self.assertEqual(submit("mocha", "r1session=test"), 403)
                self.assertEqual(submit("current", "r1session=test", "csrf"), 400)
                self.assertEqual(submit("mocha", "r1session=test", "csrf"), 200)
                save.assert_called_once_with("mocha")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_expired_public_frames_are_not_served(self):
        class StaleCollector:
            def latest(self):
                return {"panel": b"old PNG", "updated": time.time() - 60}, 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with patch("r1_web.PUBLIC_COLLECTOR", StaleCollector()):
                with self.assertRaises(HTTPError) as denied:
                    urlopen(f"http://127.0.0.1:{server.server_port}/public/panel.png", timeout=2)
                self.assertEqual(denied.exception.code, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_favicon_is_public_svg_and_selected_in_page(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "r1_web.html").read_text(encoding="utf-8")
        favicon = (root / "r1_web_favicon.svg").read_bytes()
        self.assertIn('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', source)
        self.assertEqual(ElementTree.fromstring(favicon).tag, "{http://www.w3.org/2000/svg}svg")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/favicon.svg", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
                self.assertEqual(response.read(), favicon)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_console_page_matches_physical_order_and_requires_admin_session(self):
        source = (Path(__file__).resolve().parents[1] / "r1_web.html").read_text(encoding="utf-8")
        buttons = [f'data-page="{page}"' for page in ("panel", "fans", "console", "off")]
        self.assertEqual([source.index(button) for button in buttons],
                         sorted(source.index(button) for button in buttons))
        self.assertIn("控制台画面仅供查看", source)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        command = {"action": "page", "value": "console"}

        def submit(cookie=None, csrf=None):
            headers = {"Content-Type": "application/json",
                       "Origin": f"http://127.0.0.1:{server.server_port}"}
            if cookie:
                headers["Cookie"] = cookie
            if csrf:
                headers["X-R1-CSRF"] = csrf
            request = Request(base + "/api/panel", data=json.dumps(command).encode(), headers=headers)
            try:
                response = urlopen(request, timeout=2)
            except HTTPError as error:
                response = error
            with response:
                return response.status

        try:
            with patch.dict(SESSIONS, {"test": (time.time() + 60, "csrf")}, clear=True), \
                    patch("r1_web.request", return_value={"ok": True}) as panel_request:
                self.assertEqual(submit(), 401)
                self.assertEqual(submit("r1session=test"), 403)
                self.assertEqual(submit("r1session=test", "csrf"), 200)
                panel_request.assert_called_once_with(command)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_curve_editor_uses_percent_without_changing_raw_pwm_api(self):
        source = (Path(__file__).resolve().parents[1] / "r1_web.html").read_text(encoding="utf-8")
        self.assertEqual(source.count("<span>输出 %</span>"), 2)
        self.assertIn("for(const percent of [0,25,50,75,100])", source)
        self.assertIn("function outputPercent(pwm){return Math.round(pwm*100/255)}", source)
        self.assertIn("pct===outputPercent(point[1])?point[1]:Math.round(pct*255/100)", source)
        self.assertIn("cpu:readCurve('cpu'),drive:readCurve('drive')", source)
        self.assertIn('id="curveProfile"', source)
        self.assertIn("const profile=activeProfile,record={profile", source)
        self.assertEqual(preview_web_source(), source)

    def test_source_allowlist(self):
        # Default allowlist covers loopback, CGNAT, and all RFC1918 private
        # ranges so the panel works on any LAN subnet out of the box.
        self.assertTrue(client_allowed("192.168.50.8"))
        self.assertTrue(client_allowed("192.168.99.197"))
        self.assertTrue(client_allowed("10.0.0.5"))
        self.assertTrue(client_allowed("172.16.4.4"))
        self.assertTrue(client_allowed("100.101.1.1"))
        self.assertFalse(client_allowed("8.8.8.8"))
        self.assertFalse(client_allowed("::1"))

    def test_session_cookie_secure_flag_tracks_scheme(self):
        with TemporaryDirectory() as directory:
            admin = Path(directory) / "admin.json"
            admin.write_text(json.dumps(hash_password("password 12345")))
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def login(forwarded_proto=None):
                scheme = "https" if forwarded_proto == "https" else "http"
                headers = {"Content-Type": "application/json",
                           "Origin": f"{scheme}://127.0.0.1:{server.server_port}"}
                if forwarded_proto:
                    headers["X-Forwarded-Proto"] = forwarded_proto
                request = Request(base + "/api/login",
                                 data=json.dumps({"password": "password 12345"}).encode(), headers=headers)
                try:
                    response = urlopen(request, timeout=2)
                except HTTPError as error:
                    response = error
                with response:
                    return response.headers.get("Set-Cookie", "")

            try:
                with patch("r1_web.ADMIN_FILE", admin), patch.dict(SESSIONS, {}, clear=True), \
                        patch.dict(FAILURES, {}, clear=True), \
                        patch("r1_web.TRUSTED_PROXIES", (ipaddress.ip_network("127.0.0.1/32"),)):
                    self.assertNotIn("Secure", login())
                    self.assertIn("Secure", login(forwarded_proto="https"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_password_hash_is_salted(self):
        one = hash_password("correct horse battery staple")
        two = hash_password("correct horse battery staple")
        self.assertNotEqual(one, two)
        self.assertTrue(verify_password("correct horse battery staple", one))
        self.assertFalse(verify_password("wrong password", one))
        self.assertTrue(verify_password("12345678", hash_password("12345678")))
        with self.assertRaisesRegex(ValueError, "8–256"):
            hash_password("1234567")

    def test_password_change_requires_current_and_revokes_sessions(self):
        with TemporaryDirectory() as directory:
            admin = Path(directory) / "admin.json"
            old, new = "old password 12345", "new password 67890"
            admin.write_text(json.dumps(hash_password(old)))
            with patch("r1_web.ADMIN_FILE", admin), patch.dict(SESSIONS, {"active": (9999999999, "csrf")}, clear=True):
                with self.assertRaisesRegex(ValueError, "current password"):
                    change_admin_password("wrong password", new)
                self.assertIn("active", SESSIONS)
                self.assertTrue(verify_password(old, json.loads(admin.read_text())))
                with self.assertRaisesRegex(ValueError, "differ"):
                    change_admin_password(old, old)
                with self.assertRaisesRegex(ValueError, "8–256"):
                    change_admin_password(old, "short")
                change_admin_password(old, new)
                self.assertFalse(SESSIONS)
                record = json.loads(admin.read_text())
                self.assertFalse(verify_password(old, record))
                self.assertTrue(verify_password(new, record))
                self.assertEqual(admin.stat().st_mode & 0o777, 0o600)

    def test_password_change_http_flow_with_temporary_credentials(self):
        with TemporaryDirectory() as directory:
            admin = Path(directory) / "admin.json"
            old, new = "temporary old 12345", "temporary new 67890"
            admin.write_text(json.dumps(hash_password(old)))
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def call(path, body=None, cookie=None, csrf=None):
                headers = {}
                if body is not None:
                    headers.update({"Content-Type": "application/json",
                                    "Origin": f"http://127.0.0.1:{server.server_port}"})
                if cookie:
                    headers["Cookie"] = cookie
                if csrf:
                    headers["X-R1-CSRF"] = csrf
                request = Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
                try:
                    response = urlopen(request, timeout=2)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read()), response.headers

            try:
                with patch("r1_web.ADMIN_FILE", admin), patch.dict(SESSIONS, {}, clear=True), patch.dict(FAILURES, {}, clear=True):
                    status, data, headers = call("/api/login", {"password": old})
                    self.assertEqual(status, 200)
                    jar = cookies.SimpleCookie(headers["Set-Cookie"])
                    cookie = "r1session=" + jar["r1session"].value
                    body = {"current": old, "new": new}
                    self.assertEqual(call("/api/password", body, cookie)[0], 403)
                    self.assertEqual(call("/api/password", {"current": "wrong", "new": new}, cookie, data["csrf"])[0], 400)
                    self.assertEqual(call("/api/password", body, cookie, data["csrf"])[0], 200)
                    self.assertEqual(call("/api/status", cookie=cookie)[0], 401)
                    self.assertEqual(call("/api/login", {"password": old})[0], 401)
                    self.assertEqual(call("/api/login", {"password": new})[0], 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_encoder_emits_png(self):
        frame = bytes([0, 0, 255, 0]) * WIDTH * HEIGHT
        encoded = rgb_png(frame, WIDTH * 4)
        self.assertTrue(encoded.startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaises(ValueError):
            rgb_png(b"short", WIDTH * 4)


class LogTests(unittest.TestCase):
    def test_sanitize_strips_query_and_redacts_auth(self):
        self.assertEqual(_sanitize_access_path("/api/status?token=abc"), "/api/status")
        self.assertEqual(_sanitize_access_path("/api/login?password=secret"), "<redacted>")
        self.assertEqual(_sanitize_access_path("/api/password"), "<redacted>")
        self.assertEqual(_sanitize_access_path("/api/session?a=1"), "<redacted>")
        self.assertEqual(_sanitize_access_path("/api/frame?x=1"), "/api/frame")
        self.assertEqual(_sanitize_access_path(""), "")

    def test_web_log_writes_when_enabled(self):
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "access.log"
            with patch("r1_web.LOG_ENABLED", True), patch("r1_web.WEB_LOG_FILE", log_path):
                _web_log("%s %s", "1.2.3.4", "/api/status")
            self.assertTrue(log_path.exists())
            content = log_path.read_text()
            self.assertIn("1.2.3.4 /api/status", content)
            self.assertNotIn("token=", content)

    def test_web_log_silent_when_disabled(self):
        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "access.log"
            with patch("r1_web.LOG_ENABLED", False), patch("r1_web.WEB_LOG_FILE", log_path):
                _web_log("%s", "should-not-appear")
            self.assertFalse(log_path.exists())

    def test_controller_labels_keep_offline_switching_full_and_unknown_semantics(self):
        cases = [({'live': False}, '温控服务离线'),
                 ({'live': True, 'mode': 'auto', 'desired': 'full'}, '正在切换模式'),
                 ({'live': True, 'mode': 'full', 'desired': 'full', 'remaining': 0}, '全速散热 · 剩余 0 秒'),
                 ({'live': True, 'mode': 'full', 'desired': 'full'}, '全速散热'),
                 ({'live': True, 'mode': 'high', 'desired': 'high'}, '标准模式'),
                 ({'live': True, 'mode': 'low', 'desired': 'low'}, '静音模式'),
                 ({'live': True, 'mode': 'auto', 'desired': 'auto'}, '标准模式'),
                 ({'live': True, 'mode': 'unknown', 'desired': 'unknown'}, '曲线状态未知'),
                 ({'live': True, 'mode': 'offline', 'desired': 'offline'}, '曲线状态未知'),
                 ({'live': True, 'mode': 'switching', 'desired': 'switching'}, '曲线状态未知')]
        for control, expected in cases:
            with self.subTest(control=control):
                self.assertIn(expected.encode(), render_fans({'control': control}))

    def test_fan_sensor_fault_status_takes_priority_over_control_mode(self):
        control = {'live': True, 'mode': 'full', 'desired': 'full'}
        svg = render_fans({'control': control, 'fans': {'fan2_input': 0}})
        self.assertIn('停转'.encode(), svg)
        self.assertIn('读数不可用'.encode(), svg)


if __name__ == "__main__":
    unittest.main()

