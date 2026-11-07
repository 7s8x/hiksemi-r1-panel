#!/usr/bin/env python3
"""PVE status dashboard and local screen controls for the HIKSEMI R1."""

import argparse
import ctypes
import fcntl
import json
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import time

from r1_fan_control_ipc import control_status, request_mode
from r1_rgb_control import RgbControl
from r1_panel_runtime import MAX_COMMAND, open_control, publish_state
from r1_collection import SharedReader, read_snapshot
from r1_site_config import SITE
# Explicit compatibility exports; callers may use these module attributes.
from r1_host_sampling import (
    discover_drives as discover_drives,
    bay_identity as bay_identity,
    read_text as read_text,
    temp_for_hwmon as temp_for_hwmon,
    fan_readings as fan_readings,
    drive_temperature as drive_temperature,
    memory as memory,
    cpu_usage as cpu_usage,
    disk_identity as disk_identity,
    nvme_identity as nvme_identity,
    bay_states as bay_states,
)
from r1_panel_metrics import MinuteMetrics
from r1_framebuffer import write_frame
from r1_display_refresh import DisplayRefresh
from r1_web_views import render_fans, render_information
from r1_svg_painter import paint_svg


WIDTH = 376
HEIGHT = 960
BAY_PORTS = SITE.bay_ports
BAY_CONTROLLER = SITE.controller


STOP = False
# Keep console opposite the dark page: switching directly from a black fbdev
# frame to tty1 showed only a cursor in the first guarded four-page trial.
SCREEN_STATES = ("panel", "fans", "console", "off")
RGB = RgbControl()
_RGB_CACHE = None
_RGB_EXPIRES = 0


def remember_rgb_state(state):
    """Reuse a verified write readback; None invalidates after a hardware toggle."""
    global _RGB_CACHE, _RGB_EXPIRES
    _RGB_CACHE = state
    _RGB_EXPIRES = time.monotonic() + 15 if state is not None else 0


def rgb_state_cached(force=False):
    global _RGB_CACHE, _RGB_EXPIRES
    now = time.monotonic()
    if force or now >= _RGB_EXPIRES:
        _RGB_CACHE = None
        _RGB_EXPIRES = now + 15
        state = RGB.read_state()
        if (len(state) != 4 or state[0] not in (0, 1, 2, 3)
                or any(type(v) is not int or not 0 <= v <= 255 for v in state)):
            raise RuntimeError("RGB state unavailable")
        _RGB_CACHE = state
    if _RGB_CACHE is None:
        raise RuntimeError("RGB state unavailable")
    return _RGB_CACHE


def rgb_status():
    try:
        return rgb_state_cached()[0] != 0
    except (OSError, RuntimeError, ValueError):
        return None


def stop(_signum, _frame):
    global STOP
    STOP = True


def screen_snapshot():
    """Read the shared display snapshot without starting host queries."""
    return read_snapshot()


class Painter:
    def __init__(self):
        self.lib = ctypes.CDLL("libcairo.so.2")
        specs = {
            "cairo_image_surface_create": ([ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
            "cairo_create": ([ctypes.c_void_p], ctypes.c_void_p),
            "cairo_destroy": ([ctypes.c_void_p], None),
            "cairo_surface_destroy": ([ctypes.c_void_p], None),
            "cairo_set_source_rgb": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double], None),
            "cairo_rectangle": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double], None),
            "cairo_new_sub_path": ([ctypes.c_void_p], None),
            "cairo_arc": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double], None),
            "cairo_close_path": ([ctypes.c_void_p], None),
            "cairo_fill": ([ctypes.c_void_p], None),
            "cairo_move_to": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double], None),
            "cairo_select_font_face": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int], None),
            "cairo_set_font_size": ([ctypes.c_void_p, ctypes.c_double], None),
            "cairo_show_text": ([ctypes.c_void_p, ctypes.c_char_p], None),
            "cairo_text_extents": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p], None),
            "cairo_surface_flush": ([ctypes.c_void_p], None),
            "cairo_image_surface_get_data": ([ctypes.c_void_p], ctypes.c_void_p),
            "cairo_image_surface_get_stride": ([ctypes.c_void_p], ctypes.c_int),
            "cairo_surface_write_to_png": ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_int),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            fn.restype = result
        self.surface = self.lib.cairo_image_surface_create(0, WIDTH, HEIGHT)
        self.ctx = self.lib.cairo_create(self.surface)

    def color(self, rgb):
        self.lib.cairo_set_source_rgb(self.ctx, *(c / 255 for c in rgb))

    def rect(self, x, y, w, h, rgb):
        self.color(rgb)
        self.lib.cairo_rectangle(self.ctx, x, y, w, h)
        self.lib.cairo_fill(self.ctx)

    def text(self, x, y, value, size=18, rgb=(222, 235, 240), bold=False):
        self.color(rgb)
        self.lib.cairo_select_font_face(self.ctx, b"DejaVu Sans", 0, 1 if bold else 0)
        self.lib.cairo_set_font_size(self.ctx, size)
        self.lib.cairo_move_to(self.ctx, x, y)
        self.lib.cairo_show_text(self.ctx, str(value).encode("utf-8"))

    def draw(self, data, touch_mode=False, brightness=None, view="panel"):
        svg = render_fans(data, web_layout=True) if view == "fans" else render_information(data, web_layout=True)
        paint_svg(self, svg)
        return
    def end_screen(self):
        self.rect(0, 0, WIDTH, HEIGHT, (9, 20, 29))
        self.text(24, 82, "HIKSEMI R1  /  PVE", 16, (103, 185, 208), True)
        self.text(24, 430, "PREVIEW ENDED", 26, (231, 242, 246), True)
        self.text(24, 466, "Live data is no longer shown.", 14, (145, 169, 180))
        self.text(24, 925, "SSH AND PVE SERVICES ARE UNCHANGED", 11, (145, 169, 180))
        self.lib.cairo_surface_flush(self.surface)

    def pixels(self):
        stride = self.lib.cairo_image_surface_get_stride(self.surface)
        pointer = self.lib.cairo_image_surface_get_data(self.surface)
        return ctypes.string_at(pointer, stride * HEIGHT), stride

    def png(self, path):
        status = self.lib.cairo_surface_write_to_png(self.surface, os.fsencode(path))
        if status:
            raise RuntimeError(f"PNG write failed: Cairo status {status}")

    def close(self):
        self.lib.cairo_destroy(self.ctx)
        self.lib.cairo_surface_destroy(self.surface)


def framebuffer_info(fd):
    var = bytearray(160)
    fix = bytearray(128)
    fcntl.ioctl(fd, 0x4600, var, True)
    fcntl.ioctl(fd, 0x4602, fix, True)
    xres, yres, _, _, _, _, bpp, _ = struct.unpack_from("8I", var)
    red, green, blue = struct.unpack_from("I", var, 32)[0], struct.unpack_from("I", var, 44)[0], struct.unpack_from("I", var, 56)[0]
    line_length = struct.unpack_from("I", fix, 48)[0]
    if (xres, yres, bpp, red, green, blue) != (WIDTH, HEIGHT, 32, 16, 8, 0):
        raise RuntimeError(f"Unsupported framebuffer: {xres}x{yres} {bpp}bpp RGB offsets {red}/{green}/{blue}")
    if line_length < WIDTH * 4:
        raise RuntimeError(f"Invalid framebuffer stride: {line_length}")
    return line_length


def touchscreen_path():
    for path in glob.glob("/sys/class/input/event*"):
        if read_text(path + "/device/name") == "CST0836:00 0911:5288":
            return "/dev/input/" + Path(path).name
    raise RuntimeError("R1 touchscreen was not found")


class TouchGesture:
    """Recognize one-finger taps and horizontal/vertical swipes."""

    EVENT_SIZE = struct.calcsize("llHHi")

    def __init__(self):
        self.x = 0
        self.y = 0
        self.down = False
        self.start = None
        self.finish = False
        self.begin = False
        self.multi = False
        self.slot = 0
        self.buffer = b""

    def feed(self, chunk):
        self.buffer += chunk
        gestures = []
        while len(self.buffer) >= self.EVENT_SIZE:
            item, self.buffer = self.buffer[:self.EVENT_SIZE], self.buffer[self.EVENT_SIZE:]
            _, _, kind, code, value = struct.unpack("llHHi", item)
            if kind == 3:  # EV_ABS
                if code == 0:  # ABS_X
                    self.x = value
                elif code == 1:  # ABS_Y
                    self.y = value
                elif code == 0x2F:  # ABS_MT_SLOT
                    self.slot = value
                    if value > 0 and self.down:
                        self.multi = True
                elif code == 0x35 and self.slot == 0:  # ABS_MT_POSITION_X
                    self.x = value
                elif code == 0x36 and self.slot == 0:  # ABS_MT_POSITION_Y
                    self.y = value
            elif kind == 1 and code == 0x14A:  # BTN_TOUCH
                if value:
                    self.begin = True
                else:
                    self.finish = True
            elif kind == 0 and code == 0:  # SYN_REPORT
                if self.begin:
                    self.down = True
                    self.start = (self.x, self.y, time.monotonic())
                    self.multi = False
                    self.begin = False
                if self.finish:
                    if self.down and self.start and not self.multi:
                        sx, sy, started = self.start
                        dx, dy = self.x - sx, self.y - sy
                        duration = time.monotonic() - started
                        if duration <= 1.5 and abs(dx) >= 80 and abs(dx) > abs(dy) * 1.3:
                            gestures.append(("swipe_horizontal", dx))
                        elif duration <= 1.5 and abs(dy) >= 80 and abs(dy) > abs(dx) * 1.3:
                            gestures.append(("swipe_vertical", dy))
                        elif duration <= 0.9 and abs(dx) <= 35 and abs(dy) <= 35:
                            gestures.append(("tap", (self.x, self.y)))
                    self.down = False
                    self.start = None
                    self.finish = False
        return gestures


def switch_vt(number):
    subprocess.run(["chvt", str(number)], check=True, timeout=3)


def set_tty2_cursor(visible):
    """Hide only the panel VT's fbcon cursor; never change global fbcon mode."""
    try:
        fd = os.open("/dev/tty2", os.O_WRONLY | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            os.write(fd, b"\x1b[?25h" if visible else b"\x1b[?25l")
        finally:
            os.close(fd)
    except OSError as error:
        print(f"tty2_cursor_error={error}", flush=True)


def shifted_state(current, horizontal_delta):
    """Swipe left for the next view, right for the previous view."""
    step = 1 if horizontal_delta < 0 else -1
    return SCREEN_STATES[(SCREEN_STATES.index(current) + step) % len(SCREEN_STATES)]


def fan_button_mode(point):
    x, y = point
    if 738 <= y <= 783:
        if 32 <= x <= 180:
            return "full"
        if 196 <= x <= 344:
            return "auto"
    return None


def rgb_button_action(point):
    x, y = point
    if 882 <= y <= 927:
        if 32 <= x <= 180:
            return "on"
        if 196 <= x <= 344:
            return "off"
    return None


def run_touch(painter, initial_view="panel", metrics=None):
    backlight = Path("/sys/class/backlight/intel_backlight")
    fb_blank = Path("/sys/class/graphics/fb0/blank")
    maximum = int(read_text(backlight / "max_brightness"))
    brightness = int(read_text(backlight / "brightness"))
    if maximum <= 0:
        raise RuntimeError("Backlight maximum is invalid")
    brightness = max(round(maximum * 0.02), brightness)
    minimum = max(1, round(maximum * 0.02))
    step = max(1, round(maximum * 0.05))
    touch_fd = os.open(touchscreen_path(), os.O_RDONLY | os.O_NONBLOCK)
    fb_fd = os.open("/dev/fb0", os.O_RDWR)
    collector = SharedReader()
    listener = open_control()
    if metrics:
        metrics.set_page(initial_view)
        metrics.start()
    collector.start()
    try:
        line_length = framebuffer_info(fb_fd)
        gesture = TouchGesture()
        state = initial_view
        next_refresh = 0
        display_refresh = DisplayRefresh()
        with mmap.mmap(fb_fd, line_length * HEIGHT, mmap.MAP_SHARED, mmap.PROT_WRITE) as framebuffer:
            def blit():
                started_ns = time.monotonic_ns() if metrics else 0
                succeeded = False
                try:
                    pixels, stride = painter.pixels()
                    # The i915 fbdev write path reports damaged regions to DRM.
                    # Direct mmap stores may leave FBC displaying an old frame.
                    write_frame(fb_fd, pixels, stride, line_length, WIDTH, HEIGHT)
                    succeeded = True
                finally:
                    if metrics:
                        metrics.record("fb_write", time.monotonic_ns() - started_ns, succeeded)

            def refresh(force=False, view=None):
                started_ns = time.monotonic_ns() if metrics else 0
                data, _ = collector.latest()
                if metrics:
                    metrics.record("collect", time.monotonic_ns() - started_ns, True)
                    metrics.observe_snapshot(data)
                data["clock"] = time.strftime("%H:%M")
                target = view or state
                if target == "fans":
                    data["control"] = control_status()
                    data["rgb"] = rgb_status()
                display_refresh.refresh(painter, data, target, blit, force=force, metrics=metrics)
                # getty may re-enable its cursor between refreshes. Reassert even
                # on a content-cache hit; skipping rasterization must not skip this.
                set_tty2_cursor(False)

            def show_dashboard(target):
                if state in ("panel", "fans"):
                    refresh(force=True, view=target)
                    return
                if state == "off":
                    # Keep tty2 and the DSI link active while the backlight is
                    # dark. Re-enabling fb_blank can freeze this panel on a
                    # stale tty1 frame (i915 video_mode == 3 warning).
                    refresh(force=True, view=target)
                    (backlight / "brightness").write_text(str(brightness))
                    blit()
                    return
                # Keep the LCD dark while chvt/fbcon and fb_blank redraw the
                # framebuffer. Reveal it only after our own frame is ready.
                (backlight / "brightness").write_text("0")
                switch_vt(2)
                set_tty2_cursor(False)
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                time.sleep(0.12)
                refresh(force=True, view=target)
                (backlight / "brightness").write_text(str(brightness))
                blit()

            def show_console():
                switch_vt(1)
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))

            def show_off():
                # Paint black and extinguish the backlight without resetting
                # the DSI link; blank/unblank can leave the LCD scanout stale.
                # If tty1 owns fb0, blacking it out and then calling chvt 1
                # again cannot redraw the console. Keep the dark page on tty2.
                if state == "console":
                    (backlight / "brightness").write_text("0")
                    switch_vt(2)
                    set_tty2_cursor(False)
                painter.rect(0, 0, WIDTH, HEIGHT, (0, 0, 0))
                painter.lib.cairo_surface_flush(painter.surface)
                blit()
                (backlight / "brightness").write_text("0")

            def transition(target):
                nonlocal state, next_refresh
                if target == state:
                    return
                if metrics:
                    metrics.set_page(target)
                if target in ("panel", "fans"):
                    show_dashboard(target)
                    next_refresh = time.monotonic() + 0.2
                elif target == "console":
                    show_console()
                else:
                    show_off()
                state = target

            def handle_action(action, value):
                nonlocal brightness, next_refresh
                if action == "swipe_vertical":
                    increments = max(1, min(5, round(abs(value) / 100)))
                    brightness = min(maximum, max(minimum, brightness + (step * increments if value < 0 else -step * increments)))
                    if state != "off":
                        (backlight / "brightness").write_text(str(brightness))
                    if state in ("panel", "fans"):
                        refresh()
                elif action == "swipe_horizontal":
                    transition(shifted_state(state, value))
                elif action == "tap":
                    mode = fan_button_mode(value) if state == "fans" else None
                    rgb_action = rgb_button_action(value) if state == "fans" else None
                    if rgb_action is not None:
                        enabled = RGB.status()
                        if enabled != (rgb_action == "on"):
                            enabled = RGB.toggle()
                        remember_rgb_state(None)
                        refresh()
                        next_refresh = time.monotonic() + 1
                    elif mode is not None:
                        if not control_status()["live"]:
                            raise RuntimeError("fan controller offline")
                        request_mode(mode)
                        refresh()
                        next_refresh = time.monotonic() + 1
                else:
                    raise ValueError("unknown panel action")

            def handle_command(command):
                if not isinstance(command, dict) or set(command) != {"action", "value"}:
                    raise ValueError("invalid command")
                action, value = command["action"], command["value"]
                if action == "page":
                    if value not in SCREEN_STATES:
                        raise ValueError("invalid page")
                    transition(value)
                elif action == "tap":
                    if (not isinstance(value, list) or len(value) != 2
                            or any(type(v) is not int for v in value)
                            or not 0 <= value[0] < WIDTH or not 0 <= value[1] < HEIGHT):
                        raise ValueError("invalid tap coordinates")
                    if state not in ("panel", "fans"):
                        raise ValueError("tap unavailable on console/off")
                    handle_action("tap", tuple(value))
                elif action == "brightness":
                    if type(value) is not int or not 2 <= value <= 100:
                        raise ValueError("invalid brightness")
                    nonlocal brightness
                    brightness = max(minimum, min(maximum, round(maximum * value / 100)))
                    if state != "off":
                        (backlight / "brightness").write_text(str(brightness))
                    if state in ("panel", "fans"):
                        refresh()
                elif action == "rgb":
                    if (not isinstance(value, list) or len(value) != 4
                            or any(type(v) is not int for v in value)):
                        raise ValueError("invalid RGB request")
                    current = RGB.read_state()
                    if value[0] in (1, 3) or tuple(value[1:]) != current[1:]:
                        if not Path("/var/lib/r1-web/rgb-verified").exists():
                            raise ValueError("color and effect modes await visual verification")
                    actual = RGB.set_state(*value)
                    remember_rgb_state(actual)
                    if state == "fans":
                        refresh()
                    return {"ok": True, "state": state, "rgb": actual}
                elif action == "rgb_state":
                    if value is not None:
                        raise ValueError("invalid RGB status request")
                    return {"ok": True, "state": state, "rgb": rgb_state_cached()}
                else:
                    raise ValueError("unknown command")
                return {"ok": True, "state": state}

            def refresh_delay():
                status = control_status() if state == "fans" else None
                fast = bool(status and status["live"]
                            and (status["desired"] != status["mode"] or status["mode"] == "full"))
                return 1 if fast else 15

            try:
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))
                switch_vt(2)
                set_tty2_cursor(False)
                time.sleep(0.35)
                refresh()
                next_refresh = time.monotonic() + 15
                while not STOP:
                    publish_state(state, round(100 * brightness / maximum))
                    timeout = min(1, max(0, next_refresh - time.monotonic()))
                    readable, _, _ = select.select([touch_fd, listener], [], [], timeout)
                    if listener in readable:
                        connection, _ = listener.accept()
                        with connection:
                            connection.settimeout(0.2)
                            try:
                                payload = connection.recv(MAX_COMMAND + 1)
                                if len(payload) > MAX_COMMAND:
                                    raise ValueError("command too large")
                                response = handle_command(json.loads(payload.decode("utf-8")))
                            except (OSError, ValueError, TypeError, RuntimeError) as error:
                                response = {"ok": False, "error": str(error)[:120]}
                            connection.sendall(json.dumps(response).encode("utf-8"))
                    if touch_fd in readable:
                        try:
                            events = gesture.feed(os.read(touch_fd, 24 * 64))
                        except BlockingIOError:
                            events = []
                        for action, value in events:
                            try:
                                handle_action(action, value)
                            except (OSError, RuntimeError, ValueError) as error:
                                print(f"gesture={action} error={error}", flush=True)
                    if state in ("panel", "fans") and time.monotonic() >= next_refresh:
                        delay = refresh_delay()
                        refresh()
                        next_refresh = time.monotonic() + delay
            finally:
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))
                set_tty2_cursor(True)
                switch_vt(1)
    finally:
        collector.stop()
        if metrics:
            metrics.stop()
        listener.close()
        os.close(fb_fd)
        os.close(touch_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", help="Write a PNG; do not touch the screen")
    parser.add_argument("--view", choices=("panel", "fans"), default="panel",
                        help="Dashboard page to render in preview mode")
    parser.add_argument("--initial-view", choices=("panel", "fans"), default="panel",
                        help="Dashboard page shown first in touch mode")
    parser.add_argument("--once", action="store_true", help="Draw once and exit")
    parser.add_argument("--touch", action="store_true", help="Enable tap/vertical-swipe trial")
    parser.add_argument("--metrics", action="store_true", help="Emit optional one-minute operation metrics")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    painter = Painter()
    try:
        if args.touch:
            run_touch(painter, initial_view=args.initial_view, metrics=MinuteMetrics() if args.metrics else None)
            return
        if args.preview:
            painter.draw(screen_snapshot(), view=args.view)
            painter.png(args.preview)
            return
        fd = os.open("/dev/fb0", os.O_RDWR)
        try:
            line_length = framebuffer_info(fd)
            with mmap.mmap(fd, line_length * HEIGHT, mmap.MAP_SHARED, mmap.PROT_WRITE) as framebuffer:
                def blit():
                    pixels, stride = painter.pixels()
                    for y in range(HEIGHT):
                        framebuffer[y * line_length:y * line_length + WIDTH * 4] = pixels[y * stride:y * stride + WIDTH * 4]

                while not STOP:
                    painter.draw(screen_snapshot())
                    blit()
                    if args.once:
                        break
                    for _ in range(15):
                        if STOP:
                            break
                        time.sleep(1)
                if STOP:
                    painter.end_screen()
                    blit()
        finally:
            os.close(fd)
    finally:
        painter.close()


if __name__ == "__main__":
    main()
