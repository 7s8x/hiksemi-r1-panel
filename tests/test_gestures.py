"""Offline checks for R1 touch classification and view ordering."""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    # Local workspace keeps the sanitized export alongside the live-machine copy.
    from r1_panel_repository import TouchGesture, fan_button_mode, fan_readings, rgb_button_action, shifted_state  # noqa: E402
except ModuleNotFoundError:
    from r1_panel_buttons import TouchGesture, fan_button_mode, fan_readings, shifted_state  # noqa: E402


def event(kind, code, value):
    return struct.pack("llHHi", 0, 0, kind, code, value)


def gesture(start, end):
    touch = TouchGesture()
    press = (
        event(3, 0x35, start[0])
        + event(3, 0x36, start[1])
        + event(1, 0x14A, 1)
        + event(0, 0, 0)
    )
    release = (
        event(3, 0x35, end[0])
        + event(3, 0x36, end[1])
        + event(1, 0x14A, 0)
        + event(0, 0, 0)
    )
    touch.feed(press)
    return touch.feed(release)


class GestureTests(unittest.TestCase):
    def test_left_and_right_swipes(self):
        self.assertEqual(gesture((300, 450), (150, 450)), [("swipe_horizontal", -150)])
        self.assertEqual(gesture((70, 450), (220, 450)), [("swipe_horizontal", 150)])

    def test_vertical_swipes_still_control_brightness(self):
        self.assertEqual(gesture((170, 600), (170, 400)), [("swipe_vertical", -200)])
        self.assertEqual(gesture((170, 400), (170, 600)), [("swipe_vertical", 200)])

    def test_tap_retains_button_coordinates(self):
        self.assertEqual(gesture((170, 400), (175, 404)), [("tap", (175, 404))])

    def test_fan_buttons_only_accept_taps_inside_their_bounds(self):
        self.assertEqual(fan_button_mode((100, 720)), "full")
        self.assertEqual(fan_button_mode((250, 720)), "auto")
        self.assertIsNone(fan_button_mode((188, 720)))
        self.assertIsNone(fan_button_mode((100, 760)))
        self.assertEqual(rgb_button_action((100, 918)), "on")
        self.assertEqual(rgb_button_action((250, 918)), "off")
        self.assertIsNone(rgb_button_action((188, 918)))

    def test_diagonal_drag_does_not_switch_view(self):
        self.assertEqual(gesture((80, 350), (230, 470)), [])

    def test_view_cycle(self):
        states = ("panel", "fans", "console", "off")
        self.assertEqual([shifted_state(state, -120) for state in states],
                         ["fans", "console", "off", "panel"])
        self.assertEqual([shifted_state(state, 120) for state in states],
                         ["off", "panel", "fans", "console"])
        self.assertEqual(shifted_state("console", -120), "off")
        self.assertEqual(shifted_state("off", 120), "console")


class FanReadingTests(unittest.TestCase):
    def test_read_verified_channels_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chip = root / "hwmon7"
            chip.mkdir()
            for name, value in {"name": "it8613", "fan2_input": "997",
                                "fan3_input": "2872", "pwm2": "120", "pwm3": "120",
                                "pwm2_enable": "1", "pwm3_enable": "1"}.items():
                (chip / name).write_text(value)
            self.assertEqual(fan_readings(root)["fan3_input"], 2872)
            self.assertEqual((chip / "pwm2").read_text(), "120")
            (chip / "fan2_input").write_text("0")
            self.assertEqual(fan_readings(root)["fan2_input"], 0)
            (chip / "fan2_input").write_text("-1")
            self.assertIsNone(fan_readings(root))
            (chip / "fan2_input").write_text("broken")
            self.assertIsNone(fan_readings(root))

    def test_missing_or_ambiguous_driver_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(fan_readings(root))
            for number in (0, 1):
                chip = root / f"hwmon{number}"
                chip.mkdir()
                (chip / "name").write_text("it8613")
            self.assertIsNone(fan_readings(root))


if __name__ == "__main__":
    unittest.main()
