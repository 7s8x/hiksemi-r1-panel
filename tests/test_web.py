"""Offline checks for HTTPS UI authentication and framebuffer encoder."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_web import client_allowed, hash_password, rgb_png, verify_password  # noqa: E402
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

    def test_encoder_emits_png(self):
        frame = bytes([0, 0, 255, 0]) * WIDTH * HEIGHT
        encoded = rgb_png(frame, WIDTH * 4)
        self.assertTrue(encoded.startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaises(ValueError):
            rgb_png(b"short", WIDTH * 4)


if __name__ == "__main__":
    unittest.main()
