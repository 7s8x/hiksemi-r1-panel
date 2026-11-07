"""Compare synthetic Mocha/Latte SVG and Cairo PNG against an explicit baseline.

Linux only; neither process reads live collector data nor opens a hardware device.
No source checkout, installation or production operation is performed here.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


CHILD = r'''
import hashlib, json, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from ui_preview import demo_data
from r1_web_views import render_information, render_fans
from r1_panel_repository import Painter
from r1_web import rgb_png
result = {}
for theme in ('mocha', 'latte'):
    for scenario in ('normal', 'missing', 'full'):
        data = demo_data()
        data['clock'] = '12:34'
        if scenario == 'missing':
            data.update(cpu=None, cpu_usage=None, mem_used=None, storage=[],
                        storage_capacity=[], storage_usage=[], fans=None, fan_basis=(None,None,0))
        elif scenario == 'full':
            data['control'] = {'live': True, 'desired': 'full', 'mode': 'full', 'remaining': 29}
            data['rgb'] = True
        for view, render in (('panel', render_information), ('fans', render_fans)):
            with patch('r1_theme.load_theme', return_value=theme):
                svg = render(data, theme=theme)
                painter = Painter()
                try:
                    painter.draw(data, view=view)
                    pixels, stride = painter.pixels()
                    png = rgb_png(pixels, stride)
                finally:
                    painter.close()
                import r1_web
                if hasattr(r1_web, 'encode_public_png'):
                    assert r1_web.encode_public_png(svg) == png, 'lazy PNG differs from physical renderer'
            key = theme + '/' + scenario + '/' + view
            result[key+'/svg'] = hashlib.sha256(svg).hexdigest()
            result[key+'/png'] = hashlib.sha256(png).hexdigest()
print(json.dumps(result, sort_keys=True))
'''


def hashes(root):
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', CHILD, str(root)],
                            check=True, capture_output=True, text=True, timeout=90)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('Cairo equivalence must run on Linux; macOS is not a substitute.')
    baseline = hashes(args.baseline.resolve())
    current = hashes(Path(__file__).resolve().parent)
    different = [name for name in baseline if current.get(name) != baseline[name]]
    if different or current.keys() != baseline.keys():
        raise SystemExit('synthetic render mismatch: ' + ', '.join(different))
    print(f'synthetic_render_hashes={len(current)} all_identical=true')


if __name__ == '__main__':
    main()
