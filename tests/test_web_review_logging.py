"""Privacy and malformed-request logging regressions found during review."""
from pathlib import Path
from tempfile import TemporaryDirectory
import socket
import stat
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import r1_web as web

class WebLoggingReviewTests(unittest.TestCase):
    def test_absolute_auth_urls_are_redacted(self):
        self.assertEqual(web._sanitize_access_path('http://user:test-secret@r1/api/login?token=example'), '<redacted>')

    def test_control_characters_cannot_inject_log_lines(self):
        self.assertNotIn('\n', web._sanitize_access_path('/status\nforged'))
        self.assertNotIn('\x1b', web._sanitize_access_path('/status\x1b[31m'))

    def test_new_log_is_owner_only(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'access.log'
            with patch.object(web, 'LOG_ENABLED', True), patch.object(web, 'WEB_LOG_FILE', target):
                web._web_log('%s', 'fixture')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_error_logging_does_not_echo_raw_request_details(self):
        h = object.__new__(web.Handler)
        h.client_address = ('127.0.0.1', 1234)
        with patch.object(web, 'LOG_ENABLED', True), patch.object(web, '_web_log') as log:
            h.log_message('Bad request syntax (%r)', 'GET /api/login/test-secret HTTP/1.1')
            self.assertNotIn('test-secret', str(log.call_args))

    def test_malformed_request_responds_without_leaking_the_request_line(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'access.log'
            server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            with patch.object(web, 'LOG_ENABLED', True), patch.object(web, 'WEB_LOG_FILE', target):
                thread.start()
                try:
                    with socket.create_connection(server.server_address, timeout=2) as client:
                        client.sendall(b'bad request /api/login/test-secret HTTP/1.1\r\n\r\n')
                        reply = client.recv(4096)
                    self.assertIn(b'400', reply.split(b'\r\n', 1)[0])
                finally:
                    server.shutdown(); server.server_close(); thread.join(timeout=2)
            self.assertNotIn('test-secret', target.read_text())

    def test_rotation_stays_bounded_and_io_errors_do_not_escape(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'access.log'
            with patch.object(web, 'LOG_ENABLED', True), patch.object(web, 'WEB_LOG_FILE', target), patch.object(web, '_WEB_LOG_MAX_BYTES', 1):
                for _ in range(7): web._web_log('fixture')
                self.assertEqual(len(list(target.parent.glob('access.log*'))), 4)
            with patch.object(web, 'LOG_ENABLED', True), patch.object(web, 'WEB_LOG_FILE', Path(directory)):
                web._web_log('fixture')

if __name__ == '__main__': unittest.main()
