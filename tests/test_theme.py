"""The shared theme is a safe device setting, never a control shortcut."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_theme import load_theme, save_theme, theme_svg  # noqa: E402


class ThemeTests(unittest.TestCase):
    def test_persist_and_invalid_config_falls_back_to_current(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "theme.json"
            self.assertEqual(load_theme(path), "current")
            save_theme("mocha", path)
            self.assertEqual(load_theme(path), "mocha")
            self.assertEqual(json.loads(path.read_text()), {"theme": "mocha"})
            with self.assertRaises(ValueError):
                save_theme("other", path)
            self.assertEqual(load_theme(path), "mocha")
            path.write_text('{"theme":"other"}', encoding="utf-8")
            self.assertEqual(load_theme(path), "current")

    def test_mocha_changes_colors_without_changing_layout_or_text(self):
        source = b'<svg width="376" height="960"><rect fill="#0b121b"/><text fill="#75c4dc">R1</text><circle stroke="#f4b46f"/></svg>'
        self.assertEqual(theme_svg(source, "current"), source)
        mocha = theme_svg(source, "mocha")
        self.assertIn(b'fill="#11111b"', mocha)
        self.assertIn(b'fill="#89dceb"', mocha)
        self.assertIn(b'stroke="#fab387"', mocha)
        self.assertIn(b'>R1</text>', mocha)
        with self.assertRaises(ValueError):
            theme_svg(source + b'<!-- #123456 -->', "mocha")

    def test_latte_matches_approved_preview_and_preserves_geometry(self):
        import xml.etree.ElementTree as ET
        source = b'<svg width="376" height="960"><rect x="3" y="4" width="65" height="25" fill="#17382f" stroke="#327861"/><circle cx="70" cy="90" r="58" stroke="#74bfff"/><text fill="#75c4dc">43</text></svg>'
        root = ET.fromstring(theme_svg(source, "latte"))
        self.assertEqual(root.attrib, {"width": "376", "height": "960"})
        badge, ring, number = list(root)
        self.assertEqual(badge.get("x"), "3")
        self.assertEqual(badge.get("fill"), "#7287fd")
        self.assertEqual(badge.get("fill-opacity"), "0.10")
        self.assertEqual(badge.get("stroke"), "#8839ef")
        self.assertEqual(ring.get("stroke"), "#ea76cb")
        self.assertEqual(ring.get("r"), "58")
        self.assertEqual(number.get("fill"), "#8839ef")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "theme.json"
            save_theme("latte", path)
            self.assertEqual(load_theme(path), "latte")


if __name__ == "__main__":
    unittest.main()
