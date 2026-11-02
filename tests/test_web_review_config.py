"""Startup and reverse-proxy trust regressions found during PR review."""
import ipaddress
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import r1_web as web

class WebConfigReviewTests(unittest.TestCase):
    def handler(self, address="192.168.50.8"):
        h = object.__new__(web.Handler)
        h.connection = object()
        h.client_address = (address, 1234)
        h.headers = {"Host": "r1.local:8080", "X-Forwarded-Proto": "https",
                     "X-Forwarded-Host": "spoofed.example", "Origin": "https://spoofed.example"}
        return h

    def test_untrusted_forwarded_headers_do_not_define_origin_or_cookie(self):
        h = self.handler()
        self.assertEqual(h._request_scheme(), "http")
        self.assertEqual(h._request_host(), "r1.local:8080")
        self.assertFalse(h._origin_ok())
        self.assertNotIn("Secure", h._session_cookie("example"))

    def test_explicit_trusted_proxy_can_supply_external_origin(self):
        h = self.handler("127.0.0.1")
        with patch.object(web, "TRUSTED_PROXIES", (ipaddress.ip_network("127.0.0.1/32"),), create=True):
            self.assertEqual(h._request_scheme(), "https")
            self.assertEqual(h._request_host(), "spoofed.example")
            self.assertTrue(h._origin_ok())
            self.assertIn("Secure", h._session_cookie("example"))

    def test_http_still_requires_initialized_admin_before_binding(self):
        with TemporaryDirectory() as directory, patch.object(web, "ADMIN_FILE", Path(directory) / "missing"), patch.object(web, "ThreadingHTTPServer") as server, patch.object(web, "SnapshotCollector"), patch("sys.argv", ["r1_web.py", "serve"]):
            with self.assertRaises(RuntimeError):
                web.main()
            server.assert_not_called()

    def test_tls_uses_custom_certificates_without_requiring_default_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            admin, cert, key = [root / name for name in ("admin", "custom.crt", "custom.key")]
            for p in (admin, cert, key): p.write_text("test fixture only")
            with patch.object(web, "ADMIN_FILE", admin), patch.object(web, "CERT_FILE", root / "missing.crt"), patch.object(web, "KEY_FILE", root / "missing.key"), patch.object(web.ssl, "SSLContext") as context, patch.object(web, "ThreadingHTTPServer") as server, patch.object(web, "SnapshotCollector"), patch("sys.argv", ["r1_web.py", "serve", "--tls", "--cert", str(cert), "--key", str(key)]):
                web.main()
                context.return_value.load_cert_chain.assert_called_once_with(str(cert), str(key))
                server.return_value.server_close.assert_called_once()

if __name__ == "__main__": unittest.main()
