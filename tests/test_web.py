"""Offline checks for HTTPS UI authentication and framebuffer encoder."""

import sys
import json
import time
from http import cookies
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_web import (FAILURES, Handler, SESSIONS, change_admin_password, client_allowed,
                    collect_public_frames, hash_password, public_snapshot, rgb_png,
                    verify_password)  # noqa: E402
from r1_panel_repository import WIDTH, HEIGHT  # noqa: E402
from ui_preview import preview_web_source  # noqa: E402


class WebTests(unittest.TestCase):
    def test_public_snapshot_masks_serials_and_vm_names(self):
        with (patch("r1_web.panel_repo.discover_drives", return_value=(("secret disk label", "disk-id"),)),
              patch("r1_web.panel_repo.memory", return_value=(16, 8)),
              patch("r1_web.panel_repo.read_text", return_value="3600.0 0"),
              patch("r1_web.panel_repo.temp_for_hwmon", return_value=42),
              patch("r1_web.panel_repo.nvme_identity", return_value=("2TB", "NVME", "SECRET-NVME")),
              patch("r1_web.panel_repo.disk_identity", return_value=("4TB", "HDD", "SECRET-DISK")),
              patch("r1_web.panel_repo.drive_temperature", return_value=35),
              patch("r1_web.panel_repo.storage_usage", return_value=[6, 87]),
              patch("r1_web.panel_repo.cpu_usage", return_value=28),
              patch("r1_web.panel_repo.vm_status", return_value="RUNNING"),
              patch("r1_web.panel_repo.fan_readings", return_value=None),
              patch("r1_web.control_status", return_value={"live": False}),
              patch("r1_web.request", return_value={"ok": True, "rgb": [2, 255, 0, 0]})):
            data = public_snapshot()
        self.assertEqual([identity[2] for identity, _ in data["storage"]], ["****", "****"])
        self.assertEqual(data["storage_usage"], [6, 87])
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
              patch("r1_web.rgb_png", return_value=b"PNG"),
              patch("r1_web.capture_frame", side_effect=AssertionError("physical framebuffer used"))):
            result = collect_public_frames()
        self.assertEqual(FakePainter.views, ["panel", "fans"])
        self.assertEqual(result["panel"], b"PNG")
        self.assertEqual(result["fans"], b"PNG")

    def test_public_images_require_no_session_but_controls_still_do(self):
        class FakeCollector:
            def latest(self):
                return {"panel": b"panel PNG", "fans": b"fans PNG", "updated": time.time()}, 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch("r1_web.PUBLIC_COLLECTOR", FakeCollector()), patch.dict(SESSIONS, {}, clear=True):
                for name, expected in (("panel", b"panel PNG"), ("fans", b"fans PNG")):
                    with urlopen(base + f"/public/{name}.png", timeout=2) as response:
                        self.assertEqual(response.headers.get_content_type(), "image/png")
                        self.assertEqual(response.read(), expected)
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
        self.assertEqual(favicon, (root / "logo-options" / "r1" / "flat.svg").read_bytes())
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
                       "Origin": f"https://127.0.0.1:{server.server_port}"}
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
        self.assertEqual(preview_web_source(), source)

    def test_source_allowlist(self):
        self.assertTrue(client_allowed("192.168.50.8"))
        self.assertTrue(client_allowed("100.101.1.1"))
        self.assertFalse(client_allowed("192.168.99.197"))
        self.assertFalse(client_allowed("8.8.8.8"))
        self.assertFalse(client_allowed("::1"))

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
                                    "Origin": f"https://127.0.0.1:{server.server_port}"})
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


if __name__ == "__main__":
    unittest.main()
