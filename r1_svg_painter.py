"""Render the two fixed Web SVG layouts on the R1 Cairo framebuffer surface.

This handles only the SVG elements used by r1_web_information.svg and
r1_web_fans.svg. It does not open the framebuffer or control any device.
"""

from __future__ import annotations

import ctypes
import math
from pathlib import Path
import xml.etree.ElementTree as ET


FONT = Path(__file__).resolve().parent / "assets/fonts/NotoSansSC-VF.ttf"
FONT_FAMILY = b"Noto Sans SC"
_font_loaded = False


class TextExtents(ctypes.Structure):
    _fields_ = [(name, ctypes.c_double) for name in (
        "x_bearing", "y_bearing", "width", "height", "x_advance", "y_advance")]


def _load_font():
    global _font_loaded
    if _font_loaded:
        return
    if not FONT.is_file():
        raise RuntimeError(f"front-panel font asset missing: {FONT}")
    fontconfig = ctypes.CDLL("libfontconfig.so.1")
    fontconfig.FcConfigGetCurrent.argtypes = []
    fontconfig.FcConfigGetCurrent.restype = ctypes.c_void_p
    fontconfig.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    fontconfig.FcConfigAppFontAddFile.restype = ctypes.c_int
    config = fontconfig.FcConfigGetCurrent()
    if not config or not fontconfig.FcConfigAppFontAddFile(config, str(FONT).encode()):
        raise RuntimeError("could not load the project font into Fontconfig")
    _font_loaded = True


def _setup(lib):
    signatures = {
        "cairo_save": ([ctypes.c_void_p], None),
        "cairo_restore": ([ctypes.c_void_p], None),
        "cairo_new_path": ([ctypes.c_void_p], None),
        "cairo_fill_preserve": ([ctypes.c_void_p], None),
        "cairo_stroke": ([ctypes.c_void_p], None),
        "cairo_set_source_rgba": ([ctypes.c_void_p] + [ctypes.c_double] * 4, None),
        "cairo_set_line_width": ([ctypes.c_void_p, ctypes.c_double], None),
        "cairo_set_line_cap": ([ctypes.c_void_p, ctypes.c_int], None),
        "cairo_line_to": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double], None),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = result


def _color(lib, ctx, value, opacity=1.0):
    if value in (None, "none"):
        return False
    red, green, blue = (int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))
    lib.cairo_set_source_rgba(ctx, red, green, blue, float(opacity))
    return True


def _round_path(lib, ctx, x, y, width, height, radius):
    lib.cairo_new_path(ctx)
    radius = min(radius, width / 2, height / 2)
    if radius <= 0:
        lib.cairo_rectangle(ctx, x, y, width, height)
        return
    for cx, cy, start in (
        (x + width - radius, y + radius, -math.pi / 2),
        (x + width - radius, y + height - radius, 0),
        (x + radius, y + height - radius, math.pi / 2),
        (x + radius, y + radius, math.pi),
    ):
        lib.cairo_arc(ctx, cx, cy, radius, start, start + math.pi / 2)
    lib.cairo_close_path(ctx)


def _rect(lib, ctx, element):
    x, y = float(element.get("x", 0)), float(element.get("y", 0))
    width, height = float(element.get("width", 0)), float(element.get("height", 0))
    if width <= 0 or height <= 0:
        return
    lib.cairo_save(ctx)
    _round_path(lib, ctx, x, y, width, height, float(element.get("rx", 0)))
    if _color(lib, ctx, element.get("fill"), element.get("fill-opacity", 1)):
        if element.get("stroke"):
            lib.cairo_fill_preserve(ctx)
        else:
            lib.cairo_fill(ctx)
    if _color(lib, ctx, element.get("stroke"), element.get("stroke-opacity", 1)):
        lib.cairo_set_line_width(ctx, float(element.get("stroke-width", 1)))
        lib.cairo_stroke(ctx)
    lib.cairo_restore(ctx)


def _circle(lib, ctx, element):
    x, y, radius = (float(element.get(key, 0)) for key in ("cx", "cy", "r"))
    if radius <= 0:
        return
    lib.cairo_save(ctx)
    lib.cairo_new_path(ctx)
    lib.cairo_arc(ctx, x, y, radius, 0, 2 * math.pi)
    if _color(lib, ctx, element.get("fill"), element.get("fill-opacity", 1)):
        if element.get("stroke"):
            lib.cairo_fill_preserve(ctx)
        else:
            lib.cairo_fill(ctx)
    if _color(lib, ctx, element.get("stroke"), element.get("stroke-opacity", 1)):
        dash = element.get("stroke-dasharray")
        if dash:
            length = float(dash.split()[0])
            if length <= 0:
                lib.cairo_restore(ctx)
                return
            # Both templates rotate their progress arc to start at 12 o'clock.
            if not element.get("transform", "").startswith("rotate(-90 "):
                raise ValueError("unexpected SVG ring rotation")
            lib.cairo_new_path(ctx)
            lib.cairo_arc(ctx, x, y, radius, -math.pi / 2,
                          -math.pi / 2 + min(length / radius, 2 * math.pi))
        lib.cairo_set_line_width(ctx, float(element.get("stroke-width", 1)))
        lib.cairo_set_line_cap(ctx, 1 if element.get("stroke-linecap") == "round" else 0)
        lib.cairo_stroke(ctx)
    lib.cairo_restore(ctx)


def _line(lib, ctx, element):
    lib.cairo_save(ctx)
    if _color(lib, ctx, element.get("stroke"), element.get("stroke-opacity", 1)):
        lib.cairo_new_path(ctx)
        lib.cairo_move_to(ctx, float(element.get("x1")), float(element.get("y1")))
        lib.cairo_line_to(ctx, float(element.get("x2")), float(element.get("y2")))
        lib.cairo_set_line_width(ctx, float(element.get("stroke-width", 1)))
        lib.cairo_stroke(ctx)
    lib.cairo_restore(ctx)


def _segments(element):
    segments = []
    if element.text:
        segments.append((element.text, element))
    for child in element:
        if child.tag.rsplit("}", 1)[-1] != "tspan":
            raise ValueError("unexpected SVG text child")
        if child.text:
            segments.append((child.text, child))
        if child.tail:
            segments.append((child.tail, element))
    return segments


def _text(lib, ctx, element):
    x, y = float(element.get("x", 0)), float(element.get("y", 0))
    base_size = float(element.get("font-size", 14))
    if element.get("dominant-baseline") == "middle":
        y += base_size * 0.35
    bold = element.get("font-weight") in ("600", "700", "bold")
    segments = _segments(element)
    if not segments:
        return
    lib.cairo_save(ctx)
    widths = []
    for value, span in segments:
        size = float(span.get("font-size", base_size))
        lib.cairo_select_font_face(ctx, FONT_FAMILY, 0, 1 if bold else 0)
        lib.cairo_set_font_size(ctx, size)
        extent = TextExtents()
        lib.cairo_text_extents(ctx, value.encode("utf-8"), ctypes.byref(extent))
        widths.append(extent.x_advance)
    total = sum(widths)
    anchor = element.get("text-anchor")
    if anchor == "end":
        x -= total
    elif anchor == "middle":
        x -= total / 2
    for (value, span), width in zip(segments, widths):
        size = float(span.get("font-size", base_size))
        lib.cairo_select_font_face(ctx, FONT_FAMILY, 0, 1 if bold else 0)
        lib.cairo_set_font_size(ctx, size)
        _color(lib, ctx, span.get("fill", element.get("fill")))
        lib.cairo_move_to(ctx, x, y)
        lib.cairo_show_text(ctx, value.encode("utf-8"))
        x += width
    lib.cairo_restore(ctx)


def paint_svg(painter, svg: bytes):
    """Paint a rendered, trusted Web template into an existing Cairo surface."""
    _load_font()
    lib, ctx = painter.lib, painter.ctx
    _setup(lib)
    root = ET.fromstring(svg)
    if root.get("width") != "376" or root.get("height") != "960":
        raise ValueError("unexpected front-panel SVG dimensions")
    methods = {"rect": _rect, "circle": _circle, "line": _line, "text": _text}
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in ("svg", "g", "tspan"):
            continue
        if name not in methods:
            raise ValueError(f"unexpected SVG element: {name}")
        methods[name](lib, ctx, element)
    lib.cairo_surface_flush(painter.surface)
