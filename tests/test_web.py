"""Offline checks for HTTPS UI authentication and framebuffer encoder."""

import sys
import json
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
                    hash_password, rgb_png, verify_password)  # noqa: E402
from r1_panel_repository import WIDTH, HEIGHT  # noqa: E402


class WebTests(unittest.TestCase):
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
