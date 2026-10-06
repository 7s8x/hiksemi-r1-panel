"""Safety envelope and persistence tests for editable fan curves."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_fan_config import DEFAULT, curves, digest, load, save, validate  # noqa: E402


class FanConfigTests(unittest.TestCase):
    def test_defaults_match_existing_controller(self):
        cpu, drive = curves(DEFAULT)
        self.assertEqual(cpu.pwm_at(70), 140)
        self.assertEqual(drive.pwm_at(50), 180)
        self.assertEqual(cpu.pwm_at(95), 255)

    def test_rejects_unsafe_curve(self):
        for field, replacement in (("cpu", [[55, 119], [95, 255]]),
                                   ("cpu", [[55, 120], [90, 250]]),
                                   ("cpu", [[55, 120], [90, 120], [95, 255]]),
                                   ("drive", [[40, 140], [38, 255]]),
                                   ("drive", [[40, 150], [60, 140]])):
            record = copy.deepcopy(DEFAULT)
            record[field] = replacement
            with self.assertRaises(ValueError):
                validate(record)

    def test_atomic_save_and_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "curves.json"
            saved = save(DEFAULT, path)
            self.assertEqual(load(path), saved)
            self.assertEqual(digest(load(path)), digest(DEFAULT))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
