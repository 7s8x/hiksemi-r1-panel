"""Offline checks for narrow RGB trial guardrails; no I2C access."""

import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_rgb_trial import BASELINE, run_trial, set_state, write_byte_data  # noqa: E402
from r1_rgb_control import RgbControl  # noqa: E402
try:
    from r1_panel_repository import rgb_button_hit  # noqa: E402
except ModuleNotFoundError:
    from r1_panel_buttons import rgb_button_hit  # noqa: E402


class RgbTrialTests(unittest.TestCase):
    def test_screen_button_bounds(self):
        self.assertTrue(rgb_button_hit((280, 850)))
        self.assertTrue(rgb_button_hit((318, 881)))
        self.assertFalse(rgb_button_hit((280, 810)))
        self.assertFalse(rgb_button_hit((180, 850)))

    @patch("r1_rgb_control.os.close")
    @patch("r1_rgb_control.write_byte_data")
    @patch("r1_rgb_control.read_byte_data", side_effect=[2, 0, 0, 2])
    def test_toggle_off_then_restore_previous_mode(self, read, write, _close):
        control = RgbControl()
        control._open = lambda: 9
        self.assertFalse(control.toggle())
        self.assertTrue(control.toggle())
        self.assertEqual([call.args[2] for call in write.call_args_list], [0, 2])

    def test_rejects_unlisted_register_and_bad_value(self):
        with self.assertRaises(ValueError):
            write_byte_data(-1, 0x91, 1)
        with self.assertRaises(ValueError):
            write_byte_data(-1, 0x90, 256)

    @patch("r1_rgb_trial.read_state", return_value=(1, 0, 0, 64))
    @patch("r1_rgb_trial.write_byte_data")
    def test_set_state_writes_colors_before_mode(self, write, _read):
        set_state(-1, 1, 0, 0, 64)
        self.assertEqual(
            write.call_args_list,
            [call(-1, 0x92, 0), call(-1, 0x93, 0),
             call(-1, 0x94, 64), call(-1, 0x90, 1)],
        )

    @patch("r1_rgb_trial.time.sleep")
    @patch("r1_rgb_trial.set_state")
    @patch("r1_rgb_trial.read_byte_data", return_value=0)
    @patch("r1_rgb_trial.write_byte_data")
    @patch("r1_rgb_trial.read_state", return_value=BASELINE)
    def test_trial_restores_original_even_on_error(self, _read, _write, _mode, set_rgb, _sleep):
        set_rgb.side_effect = [RuntimeError("test failure"), None]
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            run_trial(-1)
        self.assertEqual(set_rgb.call_args_list[-1], call(-1, *BASELINE))


if __name__ == "__main__":
    unittest.main()
