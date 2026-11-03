"""Offline tests for conservative R1 temperature-to-PWM policy."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_fan_curve import Reading, controllable_mode, next_pwm, restore, run, target_pwm, target_pwms  # noqa: E402


def reading(cpu=62, drive=40, drives=4, fan2=995, fan3=2860):
    return Reading(cpu, drive, drives, fan2, fan3, 120, 120, 1, 1)


class FanCurveTests(unittest.TestCase):
    def setUp(self):
        # Unit tests must never publish to an actual controller or consume its mode file.
        for name, value in (("publish_status", None), ("requested_mode", "auto")):
            patched = patch("r1_fan_curve." + name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)
        from tempfile import TemporaryDirectory
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = patch("r1_fan_curve.CONFIG_FILE", Path(temporary.name) / "absent-curves.json")
        config.start()
        self.addCleanup(config.stop)

    def test_normal_temperature_keeps_baseline(self):
        self.assertEqual(target_pwm(reading(), 4), 120)

    def test_either_heat_source_increases_both_channels(self):
        self.assertEqual(target_pwm(reading(cpu=70), 4), 140)
        self.assertEqual(target_pwm(reading(drive=50), 4), 180)
        self.assertEqual(target_pwm(reading(cpu=95), 4), 255)

    def test_independent_curves_and_cpu_assist(self):
        self.assertEqual(target_pwms(reading(cpu=70), 4), (120, 140))
        self.assertEqual(target_pwms(reading(cpu=62, drive=50), 4), (180, 120))
        self.assertEqual(target_pwms(reading(cpu=80), 4), (120, 180))
        self.assertEqual(target_pwms(reading(cpu=85), 4), (160, 200))
        self.assertEqual(target_pwms(reading(cpu=90), 4), (220, 220))
        self.assertEqual(target_pwms(reading(cpu=95), 4), (255, 255))
        self.assertEqual(target_pwms(reading(cpu=None), 4), (255, 255))
        self.assertEqual(target_pwms(reading(fan2=0), 4), (255, 255))
        self.assertEqual(target_pwms(reading(drives=3), 4), (255, 255))

    def test_missing_sensor_or_zero_rpm_fails_high(self):
        self.assertEqual(target_pwm(reading(cpu=None), 4), 255)
        self.assertEqual(target_pwm(reading(drives=3), 4), 255)
        self.assertEqual(target_pwm(reading(fan3=0), 4), 255)

    def test_channel_outputs_and_driver_full_speed_readback(self):
        from tempfile import TemporaryDirectory
        from r1_fan_curve import set_channels
        with TemporaryDirectory() as directory:
            chip = Path(directory)
            for channel in (2, 3):
                (chip / f"pwm{channel}").write_text("255")
                (chip / f"pwm{channel}_enable").write_text("0")
            set_channels(chip, 120, 200)
            self.assertEqual((chip / "pwm2").read_text().strip(), "120")
            self.assertEqual((chip / "pwm3").read_text().strip(), "200")
            self.assertEqual((chip / "pwm2_enable").read_text().strip(), "1")
            with self.assertRaises(ValueError):
                set_channels(chip, 119, 255)

    def test_increase_immediately_decrease_slowly(self):
        self.assertEqual(next_pwm(120, 180), 180)
        self.assertEqual(next_pwm(180, 120), 175)
        self.assertEqual(next_pwm(125, 120), 120)
        self.assertEqual(next_pwm(124, 120), 120)
        self.assertEqual(next_pwm(120, 122), 122)

    def test_driver_full_speed_mode_is_legal_only_at_full_pwm(self):
        self.assertTrue(controllable_mode(1, 120))
        self.assertTrue(controllable_mode(0, 255))
        self.assertFalse(controllable_mode(0, 120))
        self.assertFalse(controllable_mode(2, 255))

    @patch("r1_fan_curve.set_both")
    @patch("r1_fan_curve.read_int", return_value=1000)
    @patch("r1_fan_curve.temperatures", return_value=(62, 40, 4))
    def test_safe_restore_returns_to_baseline(self, _temps, _read, write):
        restore(Path("/unused"))
        write.assert_called_once_with(Path("/unused"), 120)

    @patch("r1_fan_curve.set_both")
    @patch("r1_fan_curve.read_int", return_value=0)
    @patch("r1_fan_curve.temperatures", return_value=(90, 55, 4))
    def test_unsafe_restore_keeps_full_speed(self, _temps, _read, write):
        restore(Path("/unused"))
        write.assert_called_once_with(Path("/unused"), 255)

    @patch("r1_fan_curve.set_both")
    @patch("r1_fan_curve.read_int", return_value=1000)
    @patch("r1_fan_curve.temperatures", return_value=(62, 40, 3))
    def test_missing_drive_prevents_baseline_restore(self, _temps, _read, write):
        restore(Path("/unused"))
        write.assert_called_once_with(Path("/unused"), 255)

    @patch("r1_fan_curve.restore")
    @patch("r1_fan_curve.set_channels")
    @patch("r1_fan_curve.time.monotonic", side_effect=(0, 0, 1, 1))
    @patch("r1_fan_curve.sample")
    def test_restart_from_above_baseline_gradually_returns_to_curve(
            self, sample, _clock, write, _restore):
        hot_start = replace(reading(), pwm2=124, pwm3=124)
        sample.side_effect = (hot_start, hot_start)
        run(1, Path("/unused"))
        write.assert_called_once_with(Path("/unused"), 120, 120)

    @patch("r1_fan_curve.restore")
    @patch("r1_fan_curve.set_channels")
    @patch("r1_fan_curve.time.monotonic", side_effect=(0, 0, 1, 1))
    @patch("r1_fan_curve.sample")
    def test_missing_drive_at_start_requests_full_speed(
            self, sample, _clock, write, _restore):
        missing_drive = reading(drives=3)
        sample.side_effect = (missing_drive, missing_drive)
        run(1, Path("/unused"))
        write.assert_called_once_with(Path("/unused"), 255, 255)

    @patch("r1_fan_curve.restore")
    @patch("r1_fan_curve.publish_status")
    @patch("r1_fan_curve.requested_mode", return_value="full")
    @patch("r1_fan_curve.set_channels")
    @patch("r1_fan_curve.time.monotonic", side_effect=(0, 0, 1, 1))
    @patch("r1_fan_curve.sample", return_value=reading())
    def test_full_speed_request_overrides_curve(
            self, _sample, _clock, write, _mode, status, _restore):
        run(1, Path("/unused"))
        write.assert_called_once_with(Path("/unused"), 255, 255)
        status.assert_called_once_with("full", 255, pwm2=255, pwm3=255, curve_id=__import__("r1_fan_config").digest(__import__("r1_fan_config").DEFAULT))


if __name__ == "__main__":
    unittest.main()
