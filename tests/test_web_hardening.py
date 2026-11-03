"""Real loopback framing, concurrency, and initialization regression tests."""
from contextlib import contextmanager, ExitStack
import http.client
import json
from pathlib import Path
import socket
import ssl
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import r1_web as web


@contextmanager
def serving(handler=web.Handler, **kwargs):
    server = web.BoundedHTTPServer(("127.0.0.1", 0), handler, **kwargs)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    with patch.object(web, "LOG_ENABLED", False):
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


def raw_request(server, payload, half_close=False):
    with socket.create_connection(server.server_address, timeout=2) as client:
        client.sendall(payload)
        if half_close:
            client.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            data = client.recv(65536)
            if not data:
                return response
            response += data


class WebHardeningTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture generation requires installed openssl")
    def test_real_tls_handshake_and_http_response(self):
        with TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                            "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
                            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                            "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=10)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            client_context = ssl.create_default_context(cafile=str(cert))
            with serving(context=server_context) as server:
                connection = http.client.HTTPSConnection(*server.server_address, timeout=2, context=client_context)
                try:
                    connection.request("GET", "/api/theme")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn("choices", json.loads(response.read()))
                finally:
                    connection.close()

    def test_http_init_needs_no_openssl_and_password_cleanup(self):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            for name, value in (("STATE_DIR", root), ("ADMIN_FILE", root / "admin.json"),
                                ("INITIAL_FILE", root / "initial")):
                stack.enter_context(patch.object(web, name, value))
            run = stack.enter_context(patch.object(web.subprocess, "run", side_effect=FileNotFoundError))
            web.initialize()
            old = web.INITIAL_FILE.read_text().strip()
            with patch.dict(web.SESSIONS, {"old": (10**12, "csrf")}, clear=True):
                web.change_admin_password(old, "new example password")
                self.assertFalse(web.SESSIONS)
            self.assertFalse(web.INITIAL_FILE.exists())
            self.assertTrue(web.verify_password("new example password", json.loads(web.ADMIN_FILE.read_text())))
            run.assert_not_called()

    def test_cleanup_failure_does_not_undo_committed_password(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            admin = root / "admin.json"
            web.atomic_json(admin, web.hash_password("old example password"))
            # A directory cannot be unlinked: simulate the failure without permissions tricks.
            with patch.object(web, "ADMIN_FILE", admin), patch.object(web, "INITIAL_FILE", root), patch.object(web, "_web_log") as log:
                web.change_admin_password("old example password", "new example password")
                self.assertTrue(web.verify_password("new example password", json.loads(admin.read_text())))
                log.assert_called_once_with("operation_error initial_password_cleanup")

    def test_tls_init_uses_explicit_san(self):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            for name in ("ADMIN_FILE", "INITIAL_FILE", "CERT_FILE", "KEY_FILE"):
                stack.enter_context(patch.object(web, name, root / name))
            stack.enter_context(patch.object(web, "STATE_DIR", root))
            def openssl(args, **kwargs):
                if args[1] == "req":
                    web.CERT_FILE.write_text("fixture")
                    web.KEY_FILE.write_text("fixture")
            run = stack.enter_context(patch.object(web.subprocess, "run", side_effect=openssl))
            web.initialize(tls=True, tls_name="r1.example.test")
            self.assertIn("subjectAltName=DNS:r1.example.test", run.call_args_list[0].args[0])

    def test_keepalive_two_requests_share_socket(self):
        with serving() as server:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                for _ in range(2):
                    connection.request("GET", "/api/theme")
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(reply.version, 11)
                    reply.read()
                    current = connection.sock
                    if _ == 0:
                        first = current
                    else:
                        self.assertIs(current, first)
            finally:
                connection.close()

    def test_invalid_framing_is_closed_without_processing_pipeline(self):
        headers = (
            b"Content-Length: 2\r\nContent-Length: 2\r\n", b"Content-Length: -1\r\n",
            b"Content-Length: 3,3\r\n", b"Transfer-Encoding: chunked\r\n",
            b"Content-Length: 5000\r\n", b"Expect: 100-continue\r\n",
        )
        with serving() as server:
            for extra in headers:
                with self.subTest(extra=extra):
                    response = raw_request(server, b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\n" + extra + b"\r\n{}GET /api/theme HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                    self.assertNotIn(b"200 OK", response)
                    self.assertEqual(response.count(b"HTTP/1.1 "), 1)

    def test_short_body_and_rejected_origin_cannot_be_reused(self):
        with serving() as server:
            port = str(server.server_port).encode()
            origin = b"Origin: http://127.0.0.1:" + port + b"\r\n"
            response = raw_request(server, b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1:" + port + b"\r\n" + origin + b"Content-Length: 50\r\n\r\n{}", half_close=True)
            self.assertIn(b"400 Bad Request", response)
            response = raw_request(server, b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://evil.test\r\nContent-Length: 2\r\n\r\n{}GET /api/theme HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertIn(b"403 Forbidden", response)
            self.assertNotIn(b"200 OK", response)

    def test_body_timeout_is_bounded(self):
        class QuickHandler(web.Handler):
            timeout = .05
        with serving(QuickHandler) as server:
            port = str(server.server_port).encode()
            response = raw_request(server, b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1:" + port + b"\r\nOrigin: http://127.0.0.1:" + port + b"\r\nContent-Length: 50\r\n\r\n{")
            self.assertIn(b"408 Request Timeout", response)

    def test_connection_limit_and_released_slot(self):
        entered, release = threading.Event(), threading.Event()
        class WaitingHandler(web.Handler):
            def do_GET(self):
                entered.set()
                release.wait(2)
                self.close_connection = True
                self._json(200, {"ok": True})
        with serving(WaitingHandler, max_connections=1) as server:
            with socket.create_connection(server.server_address, timeout=2) as first:
                first.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                self.assertTrue(entered.wait(1))
                response = raw_request(server, b"")
                self.assertEqual(response, b"")
                release.set()
                self.assertIn(b"200 OK", first.recv(4096))
            # A fresh request can complete once the previous worker has released its slot.
            self.assertTrue(server._slots.acquire(timeout=1))
            server._slots.release()
            self.assertIn(b"200 OK", raw_request(server, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"))

    def test_invalid_host_denied_and_public_disable_requires_login(self):
        with serving() as server:
            response = raw_request(server, b"GET /api/theme HTTP/1.1\r\nHost: invalid/host\r\n\r\n")
            self.assertIn(b"403 Forbidden", response)
            with patch.object(web, "PUBLIC_READONLY", False):
                response = raw_request(server, b"GET /public/panel.svg HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                self.assertIn(b"401 Unauthorized", response)


if __name__ == "__main__":
    unittest.main()


class ProxyDomainTests(unittest.TestCase):
    def test_custom_domain_accepted_but_cross_origin_rejected(self):
        with serving() as server:
            conn = http.client.HTTPConnection(*server.server_address, timeout=2)
            conn.request("GET", "/api/theme", headers={"Host": "r1.7s8x.top"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.close()
            result = raw_request(server, b"POST /api/login HTTP/1.1\r\nHost: r1.7s8x.top\r\nOrigin: https://evil.example\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            self.assertIn(b"403", result.split(b"\r\n")[0])

    def test_trusted_proxy_https_origin(self):
        import ipaddress
        with patch.object(web, "TRUSTED_PROXIES", (ipaddress.ip_network("127.0.0.1/32"),)):
            with serving() as server:
                result = raw_request(server, b"POST /api/login HTTP/1.1\r\nHost: r1.7s8x.top\r\nX-Forwarded-Host: r1.7s8x.top\r\nX-Forwarded-Proto: https\r\nOrigin: https://r1.7s8x.top\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                self.assertIn(b"401", result.split(b"\r\n")[0])
                self.assertNotIn(b"host denied", result)


class FreshInstallThemeTests(unittest.TestCase):
    def test_init_selects_mocha_without_overwriting_existing_theme(self):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            for name, value in (("STATE_DIR", root), ("ADMIN_FILE", root / "admin.json"), ("INITIAL_FILE", root / "initial")):
                stack.enter_context(patch.object(web, name, value))
            web.initialize()
            theme = root / "theme.json"
            self.assertEqual(json.loads(theme.read_text())["theme"], "mocha")
            theme.write_text('{"theme":"latte"}')
            web.initialize()
            self.assertEqual(json.loads(theme.read_text())["theme"], "latte")
