"""Compile approved SVG templates once; project safe display values into DOM slots.

No hardware access, HTML fragments, or collector documents enter this protocol.
Text slots address a text node, preserving sibling tspans (including Safari memory).
"""
from functools import lru_cache
import hashlib
import json
import xml.etree.ElementTree as ET

from r1_theme import theme_svg
from r1_web_views import ROOT, TOKEN


@lru_cache(maxsize=6)
def layout(view, theme):
    name = 'information' if view == 'panel' else 'fans'
    source = (ROOT / f'r1_web_{name}_web.svg').read_bytes()
    root = ET.fromstring(theme_svg(source, theme))
    slots = []
    for node in root.iter():
        fields = [('text', node.text or '')] + list(node.attrib.items())
        for attr, value in fields:
            if not TOKEN.search(value):
                continue
            identifier = node.get('id') or f'dom-{len(slots)}'
            node.set('id', identifier)
            slots.append((identifier, attr, value, node.tag, tuple(node.attrib.items())))
            if attr == 'text':
                node.text = ' '
            else:
                node.set(attr, '0' if attr not in ('fill', 'stroke') else 'none')
    svg = ET.tostring(root, encoding='unicode')
    digest = hashlib.sha256((svg + repr(slots)).encode()).hexdigest()[:20]
    return {'svg': svg, 'bindings': [[s[0], s[1]] for s in slots], 'id': digest}, tuple(slots)


@lru_cache(maxsize=512)
def _color(theme, tag, attributes, attr, color):
    # Delegate context-dependent palette rules to the same physical renderer.
    node = ET.Element(tag, dict(attributes))
    node.set(attr, color)
    return ET.fromstring(theme_svg(ET.tostring(node), theme)).get(attr)


def project(view, values, theme):
    _, slots = layout(view, theme)
    result = []
    for _, attr, pattern, tag, attributes in slots:
        value = TOKEN.sub(lambda m: str(values[m[1]]), pattern)
        if attr in ('fill', 'stroke') and pattern != '{{RGB_SWATCH}}':
            value = _color(theme, tag, attributes, attr, value)
        result.append(value)
    return result


@lru_cache(maxsize=3)
def document_layout(theme):
    pages = {view: layout(view, theme)[0] for view in ('panel', 'fans')}
    identity = hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest()[:20]
    return {'id': identity, 'theme': theme, 'pages': pages}
