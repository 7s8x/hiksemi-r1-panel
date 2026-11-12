"""End-to-end fan profile wiring: buttons -> mode file -> curve -> rendering."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_fan_config import DEFAULT  # noqa: E402
from r1_fan_control_ipc import control_status, publish_status, request_mode  # noqa: E402
from r1_fan_curve import Reading, run  # noqa: E402
from r1_panel_repository import fan_button_mode  # noqa: E402
from r1_web_views import render_fans  # noqa: E402

GENTLE_CPU = [[55, 120], [70, 120], [80, 140], [90, 180], [95, 255]]
GENTLE_DRIVE = [[35, 120], [45, 120], [50, 140], [55, 200], [60, 255]]
LOW = copy.deepcopy(DEFAULT)
LOW["cpu"], LOW["drive"] = GENTLE_CPU, GENTLE_DRIVE


def reading(cpu=75, drive=50):
    return Reading(cpu, drive, 4, 995, 2860, 120, 120, 1, 1)


class FanProfileTests(unittest.TestCase):
    def _run_once(self, mode):
        with (patch("r1_fan_curve.publish_status"),
              patch("r1_fan_curve.requested_mode", return_value=mode),
              patch("r1_fan_curve.set_channels") as write,
              patch("r1_fan_curve.time.monotonic", side_effect=(0, 0, 1, 1)),
              patch("r1_fan_curve.sample", return_value=reading()),
              patch("r1_fan_curve.config_path"),
              patch("r1_fan_curve.bundle_digest", return_value="bundle"),
              patch("r1_fan_curve.load",
                    side_effect=lambda profile: DEFAULT if profile == "high" else LOW)):
            run(1, Path("/unused"))
        # restore() also writes 255 in run() finally; the first call is the policy target.
        return write.call_args_list[0].args[1:]

    def test_controller_selects_the_curve_for_the_active_profile(self):
        high_target = self._run_once("high")
        low_target = self._run_once("low")
        self.assertNotEqual(high_target, low_target)
        self.assertGreater(high_target[1], low_target[1])

    def test_full_speed_ignores_both_profiles(self):
        self.assertEqual(self._run_once("full"), (255, 255))

    def test_three_buttons_map_to_expected_modes_and_render(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mode_file, status_file = root / "mode", root / "status.json"
            expected = {80: ("full", "全速运行"), 188: ("high", "高速曲线运行中"),
                        294: ("low", "低速曲线运行中")}
            for x, (mode, label) in expected.items():
                with self.subTest(mode=mode):
                    self.assertEqual(fan_button_mode((x, 760)), mode)
                    request_mode(mode, mode_file)
                    publish_status(mode, 200, status_file)
                    status = control_status(mode_file, status_file)
                    self.assertIn(label.encode(), render_fans({"control": status}))
            # Full speed from the low profile must return to low, not silently to high.
            request_mode("full", mode_file)
            self.assertEqual(control_status(mode_file, status_file)["return_to"], "low")


if __name__ == "__main__":
    unittest.main()
