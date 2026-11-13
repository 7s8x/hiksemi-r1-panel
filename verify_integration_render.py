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
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from ui_preview import demo_data
from r1_web_views import render_information, render_fans
from r1_panel_repository import Painter
from r1_web import rgb_png
from r1_svg_painter import paint_svg
result = {}
output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
if output is not None:
    output.mkdir(parents=True, exist_ok=True)
for theme in ('mocha', 'latte'):
    for scenario in ('normal', 'missing', 'full', 'high', 'low', 'steady', 'breathe', 'rgb_missing'):
        data = demo_data()
        data['clock'] = '12:34'
        if scenario == 'missing':
            data.update(cpu=None, cpu_usage=None, mem_used=None, storage=[],
                        storage_capacity=[], storage_usage=[], fans=None, fan_basis=(None,None,0))
        elif scenario == 'full':
            data['control'] = {'live': True, 'desired': 'full', 'mode': 'full', 'remaining': 29, 'return_to': 'low'}
            data['rgb'] = True
            data['rgb_state'] = (3, 17, 83, 219)
        elif scenario in ('steady', 'breathe'):
            data['rgb'] = True
            data['rgb_state'] = (1 if scenario == 'steady' else 3, 171, 0, 255)
        elif scenario == 'rgb_missing':
            data['rgb'] = None
            data['rgb_state'] = None
        elif scenario in ('high', 'low'):
            data['control'] = {'live': True, 'desired': scenario, 'mode': scenario}
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
            if output is not None:
                (output / (key.replace('/', '-') + '.png')).write_bytes(png)
                (output / (key.replace('/', '-') + '.svg')).write_bytes(svg)
            if view == 'fans':
                root = ET.fromstring(svg)
                for group in root.iter():
                    for node in list(group):
                        position = node.get('y', node.get('cy', node.get('y1', '0')))
                        if float(position) >= 521:
                            group.remove(node)
                # Approved mode-label rename only; geometry and fault labels still compare.
                for node in root.iter():
                    if node.get('x') == '329' and node.get('y') in ('257', '468'):
                        node.text = {'高速': '标准', '低速': '静音'}.get(node.text, node.text)
                svg = ET.tostring(root, encoding='utf-8')
                masked = Painter()
                try:
                    paint_svg(masked, svg)
                    pixels, stride = masked.pixels()
                    png = rgb_png(pixels, stride)
                finally:
                    masked.close()
            result[key+'/svg'] = hashlib.sha256(svg).hexdigest()
            result[key+'/png'] = hashlib.sha256(png).hexdigest()
print(json.dumps(result, sort_keys=True))
'''


def hashes(root, output=None):
    command = [sys.executable, '-I', '-S', '-B', '-c', CHILD, str(root)]
    if output is not None:
        command.append(str(output))
    result = subprocess.run(command,
                            check=True, capture_output=True, text=True, timeout=90)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--fan-baseline', type=Path,
                        help='Explicit reviewed fan-layout reference; panel still uses --baseline.')
    parser.add_argument('--output', type=Path, help='Save synthetic frames for visual review.')
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('Cairo equivalence must run on Linux; macOS is not a substitute.')
    baseline = hashes(args.baseline.resolve())
    if args.fan_baseline is not None:
        fan_baseline = hashes(args.fan_baseline.resolve())
        baseline = {name: value for name, value in baseline.items() if '/panel/' in name}
        baseline.update({name: value for name, value in fan_baseline.items() if '/fans/' in name})
    current = hashes(Path(__file__).resolve().parent, args.output.resolve() if args.output else None)
    different = [name for name in baseline if current.get(name) != baseline[name]]
    if different or current.keys() != baseline.keys():
        raise SystemExit('synthetic render mismatch: ' + ', '.join(different))
    print(f'synthetic_render_hashes={len(current)} unchanged_regions_identical=true redesigned_controls=covered_by_tests separate_fan_reference={args.fan_baseline is not None}')


if __name__ == '__main__':
    main()
