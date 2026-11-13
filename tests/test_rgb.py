"""Offline checks for narrow RGB trial guardrails; no I2C access."""

import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_rgb_trial import BASELINE, run_trial, set_state, write_byte_data  # noqa: E402
from r1_rgb_control import RgbControl, rgb_bus_path  # noqa: E402
from r1_panel_repository import rgb_button_action  # noqa: E402


class RgbTrialTests(unittest.TestCase):
    def test_rgb_bus_follows_verified_adapter_not_bus_number(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            adapter = Path(directory)
            bus = adapter / 'i2c-7'
            bus.mkdir()
            (bus / 'name').write_text('Synopsys DesignWare I2C adapter\n')
            self.assertEqual(rgb_bus_path(adapter), '/dev/i2c-7')

    def test_rgb_bus_fails_closed_on_wrong_adapter(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            adapter = Path(directory)
            bus = adapter / 'i2c-0'
            bus.mkdir()
            (bus / 'name').write_text('SMBus I801 adapter\n')
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                rgb_bus_path(adapter)

    def test_screen_button_bounds(self):
        self.assertEqual(rgb_button_action((100, 905)), "breathe")
        self.assertEqual(rgb_button_action((280, 905)), "off")
        self.assertIsNone(rgb_button_action((280, 850)))
        self.assertEqual(rgb_button_action((188, 918)), "steady")

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

    def test_trial_cli_resolves_verified_adapter_before_open(self):
        import r1_rgb_trial as trial
        with patch.object(sys, 'argv', ['r1_rgb_trial.py', 'read']), \
             patch.object(trial, 'rgb_bus_path', return_value='/dev/i2c-7'), \
             patch.object(trial.os, 'open', return_value=9) as opened, \
             patch.object(trial.os, 'close') as closed, \
             patch.object(trial, 'ioctl_checked') as ioctl, \
             patch.object(trial, 'read_state', return_value=BASELINE):
            trial.main()
        self.assertEqual(opened.call_args.args[0], '/dev/i2c-7')
        ioctl.assert_called_once_with(9, trial.I2C_SLAVE, trial.RGB_ADDRESS)
        closed.assert_called_once_with(9)

    def test_trial_cli_refuses_unknown_bus_before_any_device_access(self):
        import r1_rgb_trial as trial
        with patch.object(sys, 'argv', ['r1_rgb_trial.py', 'trial']), \
             patch.object(trial, 'rgb_bus_path', side_effect=RuntimeError('adapter unavailable')), \
             patch.object(trial.os, 'open') as opened, patch.object(trial, 'write_byte_data') as write:
            with self.assertRaisesRegex(RuntimeError, 'adapter unavailable'):
                trial.main()
        opened.assert_not_called()
        write.assert_not_called()

    def test_bus_rejects_multiple_matching_adapter_children(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            adapter = Path(directory)
            for name in ('i2c-0', 'i2c-7'):
                (adapter / name).mkdir()
                (adapter / name / 'name').write_text('Synopsys DesignWare I2C adapter')
            with self.assertRaises(RuntimeError):
                rgb_bus_path(adapter)


if __name__ == "__main__":
    unittest.main()
