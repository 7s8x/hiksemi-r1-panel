"""The renderer must reject attributes it cannot faithfully draw."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from r1_svg_painter import validate_svg
from r1_web_views import render_information, render_fans


class SvgContractTests(unittest.TestCase):
    def test_current_layouts_both_themes_and_missing_data(self):
        data = {"cpu": 95, "cpu_usage": 100, "storage": [(("1TB", "NVME", "****"), 43)],
                "vms": [("VM 100", "RUNNING"), ("very long name for testing", "UNKNOWN")],
                "fans": {"fan2_input": 10000, "fan3_input": 0, "pwm2": 255, "pwm3": 0},
                "control": {"live": True, "mode": "full", "desired": "full", "remaining": 60}}
        for theme in ("mocha", "latte"):
            with patch("r1_theme.load_theme", return_value=theme):
                for item in ({}, data):
                    for render in (render_information, render_fans):
                        validate_svg(render(item))

    def test_group_transform_and_unimplemented_features_are_rejected(self):
        for child in ('<g transform="translate(1 2)"/>', '<text x="1" y="2" letter-spacing="3">x</text>',
                      '<image href="untrusted"/>'):
            with self.assertRaises(ValueError):
                validate_svg(('<svg width="376" height="960">' + child + '</svg>').encode())


if __name__ == "__main__": unittest.main()
