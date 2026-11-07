"""Bounded per-page lazy PNG encoding, coalescing concurrent identical requests."""
import threading


class PngCache:
    def __init__(self, encode):
        self.encode = encode
        self.locks = {view: threading.Lock() for view in ('panel', 'fans')}
        self.cached = {}

    def get(self, view, svg):
        with self.locks[view]:
            cached = self.cached.get(view)
            if cached is not None and cached[0] == svg:
                return cached[1]
            png = self.encode(svg)
            # An encoding failure never promotes old bytes to this SVG's cache entry.
            self.cached[view] = (svg, png)
            return png
