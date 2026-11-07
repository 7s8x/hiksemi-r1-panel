"""Content-based physical redraw decisions; no device access in this module."""
import time

from r1_svg_painter import paint_svg
from r1_web_views import render_information, render_fans


class DisplayRefresh:
    def __init__(self):
        self._shown = None

    def refresh(self, painter, data, view, blit, *, force=False, metrics=None):
        started = time.monotonic_ns() if metrics else 0
        ok = False
        try:
            svg = render_fans(data) if view == 'fans' else render_information(data)
            freshness = data.get('freshness') or {}
            status = (freshness.get('valid'), tuple(sorted((freshness.get('groups') or {}).items())))
            key = view, svg, status
            ok = True
        finally:
            if metrics:
                metrics.record('prepare', time.monotonic_ns() - started, ok)
        if not force and key == self._shown:
            if metrics:
                metrics.cache_hit()
            return False
        started = time.monotonic_ns() if metrics else 0
        ok = False
        try:
            paint_svg(painter, svg)
            ok = True
        finally:
            if metrics:
                metrics.record('render', time.monotonic_ns() - started, ok)
        blit()
        # Do not accept the new cache key until both rasterization and write succeed.
        self._shown = key
        return True
