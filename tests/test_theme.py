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


if __name__ == "__main__":
    unittest.main()
