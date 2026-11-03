"""Render synthetic layouts to files only; never opens a framebuffer or controls hardware."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from r1_panel_repository import Painter
from r1_svg_painter import paint_svg
from ui_preview import demo_svg


def verify(directory):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    results = []
    painter = Painter()
    try:
        for theme in ("mocha", "latte"):
            for page in ("panel", "fans"):
                paint_svg(painter, demo_svg(page, theme))
                raw, stride = painter.pixels()
                if stride < 376 * 4 or len(set(raw)) < 16:
                    raise RuntimeError("unexpected blank render")
                output = target / f"{theme}-{page}.png"
                painter.png(output)
                data = output.read_bytes()
                if data[:8] != b"\x89PNG\r\n\x1a\n":
                    raise RuntimeError("invalid PNG")
                results.append({"file": output.name, "sha256": hashlib.sha256(data).hexdigest(),
                                "bytes": len(data), "width": 376, "height": 960})
    finally:
        painter.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    print(json.dumps(verify(parser.parse_args().directory), indent=2))
