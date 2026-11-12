"""Offline mode request and acknowledgement checks for the three fan modes."""

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
            self.assertEqual(requested_mode(mode_file), "high")
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

    def test_full_request_expires_to_the_selected_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            mode_file = Path(temporary) / "mode"
            request_mode("low", mode_file)
            request_mode("full", mode_file)
            record = json.loads(mode_file.read_text())
            self.assertEqual(record["return_to"], "low")
            record["expires_at"] = time.time() - 1
            mode_file.write_text(json.dumps(record))
            self.assertEqual(requested_mode(mode_file), "low")
            self.assertEqual(requested_mode(mode_file, clear_expired=True), "low")
            self.assertEqual(mode_file.read_text(), "low\n")

    def test_full_request_from_high_expires_to_high(self):
        with tempfile.TemporaryDirectory() as temporary:
            mode_file = Path(temporary) / "mode"
            mode_file.write_text(json.dumps({"mode": "full", "expires_at": time.time() - 1}))
            self.assertEqual(requested_mode(mode_file), "high")
            self.assertEqual(requested_mode(mode_file, clear_expired=True), "high")
            self.assertEqual(mode_file.read_text(), "high\n")

    def test_repeated_full_burst_does_not_lose_its_return_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            mode_file = Path(temporary) / "mode"
            request_mode("low", mode_file)
            request_mode("full", mode_file)
            expired = json.loads(mode_file.read_text())
            expired["expires_at"] = time.time() - 1
            mode_file.write_text(json.dumps(expired))
            # An expired burst is still "full" in storage; a new burst must keep low.
            request_mode("full", mode_file)
            self.assertEqual(json.loads(mode_file.read_text())["return_to"], "low")

    def test_legacy_auto_requests_and_status_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mode_file = root / "mode"
            status_file = root / "status.json"
            mode_file.write_text("auto\n")
            self.assertEqual(requested_mode(mode_file), "high")
            request_mode("auto", mode_file)
            self.assertEqual(mode_file.read_text(), "high\n")
            publish_status("auto", 120, status_file)
            self.assertEqual(control_status(mode_file, status_file)["mode"], "high")

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

    def test_independent_channel_status_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            mode = path / "mode"
            status_path = path / "status"
            request_mode("high", mode)
            publish_status("high", 200, status_path, pwm2=120, pwm3=200)
            result = control_status(mode, status_path)
            self.assertTrue(result["live"])
            self.assertEqual((result["pwm2"], result["pwm3"]), (120, 200))


if __name__ == "__main__":
    unittest.main()
