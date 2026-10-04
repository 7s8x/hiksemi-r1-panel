import tempfile
import unittest
from pathlib import Path

from r1_hardware import (
    FanCurve,
    FanPoint,
    RgbEffect,
    candidate_rgb_register_plan,
    discover_hwmon,
    discover_i2c_adapters,
    require_verified_channels,
)


class FanCurveTests(unittest.TestCase):
    def test_interpolation_and_fail_high(self):
        curve = FanCurve((FanPoint(35, 100), FanPoint(65, 180), FanPoint(85, 255)))
        self.assertEqual(curve.pwm_at(20), 100)
        self.assertEqual(curve.pwm_at(50), 140)
        self.assertEqual(curve.pwm_at(85), 255)
        self.assertEqual(curve.pwm_at(None), 255)
        self.assertEqual(curve.pwm_at(999), 255)

    def test_rejects_unsafe_shape(self):
        with self.assertRaises(ValueError):
            FanCurve((FanPoint(35, 150), FanPoint(65, 100)))
        with self.assertRaises(ValueError):
            FanCurve((FanPoint(35, 100), FanPoint(35, 200)))


class ProbeTests(unittest.TestCase):
    def test_reads_mock_hwmon_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hwmon = root / "hwmon" / "hwmon5"
            hwmon.mkdir(parents=True)
            (hwmon / "name").write_text("it8628\n")
            (hwmon / "fan2_input").write_text("900\n")
            (hwmon / "fan3_input").write_text("1800\n")
            (hwmon / "pwm2").write_text("128\n")
            (hwmon / "pwm2_enable").write_text("2\n")
            devices = discover_hwmon(root / "hwmon")
            selected = require_verified_channels(devices, ("fan2_input", "fan3_input"))
            self.assertEqual(selected.fans_rpm["fan2_input"], 900)
            self.assertEqual(selected.pwm["pwm2"], 128)
            self.assertEqual(selected.pwm, {"pwm2": 128})
            self.assertEqual((hwmon / "pwm2_enable").read_text(), "2\n")

    def test_i2c_adapter_listing_does_not_scan_addresses(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = Path(temporary) / "i2c-2"
            adapter.mkdir()
            (adapter / "name").write_text("SMBus I801 adapter\n")
            self.assertEqual(discover_i2c_adapters(Path(temporary)), {2: "SMBus I801 adapter"})


class RgbPlanTests(unittest.TestCase):
    def test_off_and_solid_are_encoded_without_io(self):
        self.assertEqual(candidate_rgb_register_plan(RgbEffect("off")), ((0x90, 0),))
        self.assertEqual(
            candidate_rgb_register_plan(RgbEffect("solid", 12, 34, 56)),
            ((0x92, 12), (0x93, 34), (0x94, 56), (0x90, 1)),
        )

    def test_bad_channel_is_rejected(self):
        with self.assertRaises(ValueError):
            RgbEffect("solid", 256, 0, 0)


if __name__ == "__main__":
    unittest.main()
