"""Offline checks for R1 touch classification and view ordering."""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_panel import TouchGesture, shifted_state  # noqa: E402


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

    def test_tap_is_recognized_but_does_not_change_view(self):
        self.assertEqual(gesture((170, 400), (175, 404)), [("tap", 0)])

    def test_diagonal_drag_does_not_switch_view(self):
        self.assertEqual(gesture((80, 350), (230, 470)), [])

    def test_view_cycle(self):
        self.assertEqual([shifted_state(state, -120) for state in ("panel", "console", "off")],
                         ["console", "off", "panel"])
        self.assertEqual([shifted_state(state, 120) for state in ("panel", "console", "off")],
                         ["off", "panel", "console"])


if __name__ == "__main__":
    unittest.main()
