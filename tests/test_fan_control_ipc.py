"""Offline mode request and acknowledgement checks."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_fan_control_ipc import control_status, publish_status, request_mode, requested_mode  # noqa: E402


class FanControlIpcTests(unittest.TestCase):
    def test_mode_request_and_status_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mode_file = root / "mode"
            status_file = root / "status.json"
            self.assertEqual(requested_mode(mode_file), "auto")
            request_mode("full", mode_file)
            self.assertEqual(requested_mode(mode_file), "full")
            self.assertFalse(control_status(mode_file, status_file)["live"])
            publish_status("full", 255, status_file)
            status = control_status(mode_file, status_file)
            self.assertEqual((status["desired"], status["mode"], status["pwm"], status["live"]),
                             ("full", "full", 255, True))
            self.assertGreaterEqual(status["remaining"], 1)
            first_expiry = json.loads(mode_file.read_text())["expires_at"]
            request_mode("full", mode_file)
            self.assertEqual(json.loads(mode_file.read_text())["expires_at"], first_expiry)

    def test_full_request_expires_after_one_minute(self):
        with tempfile.TemporaryDirectory() as temporary:
            mode_file = Path(temporary) / "mode"
            mode_file.write_text(json.dumps({"mode": "full", "expires_at": time.time() - 1}))
            self.assertEqual(requested_mode(mode_file), "auto")
            self.assertEqual(requested_mode(mode_file, clear_expired=True), "auto")
            self.assertEqual(mode_file.read_text(), "auto\n")

    def test_invalid_request_fails_high_and_stale_status_is_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mode_file = root / "mode"
            status_file = root / "status.json"
            mode_file.write_text("invalid\n")
            status_file.write_text(json.dumps({"mode": "auto", "pwm": 120,
                                               "updated": time.time() - 30}))
            self.assertEqual(requested_mode(mode_file), "full")
            self.assertFalse(control_status(mode_file, status_file)["live"])

    def test_rejects_unknown_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                request_mode("off", Path(temporary) / "mode")


if __name__ == "__main__":
    unittest.main()
