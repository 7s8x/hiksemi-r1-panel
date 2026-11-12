"""Safety envelope and persistence tests for editable fan curves."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import r1_fan_config as config  # noqa: E402
from r1_fan_config import DEFAULT, bundle_digest, curves, digest, load, save, validate  # noqa: E402

# Gentler than the verified baseline between 70C and 90C, still >= 120 PWM and
# reaching full speed at 95C.  Legal only for the low profile.
GENTLE_CPU = [[55, 120], [70, 120], [80, 140], [90, 180], [95, 255]]


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

    def test_low_profile_may_be_gentler_but_high_may_not(self):
        record = copy.deepcopy(DEFAULT)
        record["cpu"] = GENTLE_CPU
        with self.assertRaises(ValueError):
            validate(record, "high")
        self.assertEqual(validate(record, "low")["cpu"], GENTLE_CPU)
        cpu, _ = curves(record, "low")
        self.assertLess(cpu.pwm_at(70), curves(DEFAULT)[0].pwm_at(70))

    def test_low_profile_keeps_hardware_floor_and_emergency_full_speed(self):
        for replacement in ([[55, 119], [95, 255]], [[55, 120], [95, 254]],
                            [[55, 120], [96, 255]]):
            record = copy.deepcopy(DEFAULT)
            record["cpu"] = replacement
            with self.subTest(replacement=replacement):
                with self.assertRaises(ValueError):
                    validate(record, "low")

    def test_profiles_use_separate_files_and_bundle_tracks_both(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            high, low = root / "curves.json", root / "curves-low.json"
            record = copy.deepcopy(DEFAULT)
            record["cpu"] = GENTLE_CPU
            with (patch.object(config, "CONFIG_FILE", high),
                  patch.object(config, "LOW_CONFIG_FILE", low)):
                save(DEFAULT, profile="high")
                save(record, profile="low")
                self.assertTrue(high.exists() and low.exists())
                self.assertEqual(load(profile="high"), DEFAULT)
                self.assertEqual(load(profile="low"), record)
                first = bundle_digest()
                save(DEFAULT, profile="low")
                self.assertNotEqual(bundle_digest(), first)

    def test_missing_low_file_falls_back_to_verified_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "LOW_CONFIG_FILE", Path(directory) / "absent.json"):
                self.assertEqual(load(profile="low"), validate(DEFAULT, "low"))


if __name__ == "__main__":
    unittest.main()
