#!/usr/bin/env python3
"""PVE status dashboard and local screen controls for the HIKSEMI R1."""

import argparse
import ctypes
import fcntl
import glob
import json
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import time


WIDTH = 376
HEIGHT = 960
def load_drives():
    """Load machine-specific disk IDs from a local, untracked JSON file."""
    path = Path(__file__).with_name("drives.json")
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    return tuple((item["label"], item["by_id"]) for item in records)


DRIVES = load_drives()
STOP = False
SCREEN_STATES = ("panel", "console", "off")


def stop(_signum, _frame):
    global STOP
    STOP = True


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def temp_for_hwmon(name):
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_text(path + "/name") == name:
            try:
                return int(read_text(path + "/temp1_input")) / 1000
            except ValueError:
                pass
    return None


def drive_temperature(by_id):
    disk = Path("/dev/disk/by-id", by_id).resolve()
    if not disk.exists():
        return None
    device = Path("/sys/block", disk.name, "device").resolve()
    for path in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_text(path + "/name") == "drivetemp" and Path(path, "device").resolve() == device:
            try:
                return int(read_text(path + "/temp1_input")) / 1000
            except ValueError:
                return None
    return None


def memory():
    values = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.strip().split()[0]) / 1024 / 1024
    return values.get("MemTotal", 0), values.get("MemTotal", 0) - values.get("MemAvailable", 0)


def vm_status(vmid):
    try:
        result = subprocess.run(
            ["qm", "status", str(vmid)], capture_output=True, text=True,
            timeout=4, check=True,
        )
        return result.stdout.strip().removeprefix("status: ").upper()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def network_address():
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", "vmbr0"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        for part in result.stdout.split():
            if part.startswith("192.") or part.startswith("10.") or part.startswith("172."):
                return part.split("/")[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return "--"


def snapshot():
    total, used = memory()
    try:
        uptime = int(float(read_text("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        uptime = 0
    return {
        "cpu": temp_for_hwmon("coretemp"),
        "nvme": temp_for_hwmon("nvme"),
        "drives": [(label, drive_temperature(disk)) for label, disk in DRIVES],
        "mem_total": total,
        "mem_used": used,
        "uptime": uptime,
        "ip": network_address(),
        "vms": [(100, vm_status(100)), (111, vm_status(111))],
        "clock": time.strftime("%H:%M"),
    }


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
            "cairo_fill": ([ctypes.c_void_p], None),
            "cairo_move_to": ([ctypes.c_void_p, ctypes.c_double, ctypes.c_double], None),
            "cairo_select_font_face": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int], None),
            "cairo_set_font_size": ([ctypes.c_void_p, ctypes.c_double], None),
            "cairo_show_text": ([ctypes.c_void_p, ctypes.c_char_p], None),
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

    def draw(self, data, touch_mode=False, brightness=None):
        bg, panel, muted, fg, cyan = (9, 20, 29), (19, 40, 52), (145, 169, 180), (231, 242, 246), (103, 185, 208)
        self.rect(0, 0, WIDTH, HEIGHT, bg)
        self.text(24, 42, "HIKSEMI R1  /  PVE", 14, cyan, True)
        self.text(24, 81, "HOST OVERVIEW", 27, fg, True)
        self.text(289, 110, data["clock"], 18, muted)
        self.rect(24, 106, 9, 9, (112, 213, 183))
        self.text(42, 117, "HOST ONLINE", 14, (112, 213, 183))

        self.rect(20, 140, 336, 182, panel)
        self.text(40, 172, "CPU PACKAGE", 15, muted)
        cpu = data["cpu"]
        self.text(38, 261, "--" if cpu is None else f"{cpu:.0f}", 78, fg, True)
        self.text(165, 261, "C", 27, fg)
        self.rect(40, 281, 296, 6, (31, 56, 68))
        if cpu is not None:
            self.rect(40, 281, min(296, 296 * max(0, cpu) / 105), 6, (247, 173, 109))
        self.text(40, 307, "LIVE SENSOR / 15 SEC REFRESH", 12, muted)

        self.text(24, 366, "HARDWARE TEMPERATURE", 17, fg, True)
        readings = [("NVME SYSTEM", data["nvme"]), *data["drives"]]
        for idx, (label, temp) in enumerate(readings):
            y = 384 + idx * 66
            self.rect(20, y, 336, 56, panel)
            self.text(36, y + 24, label, 15, fg, True)
            self.text(36, y + 44, "SENSOR UNAVAILABLE" if temp is None else "CURRENT TEMPERATURE", 10, muted)
            self.text(276, y + 35, "--" if temp is None else f"{temp:.0f}C", 21, cyan, True)

        self.text(24, 760, "SYSTEM", 17, fg, True)
        self.rect(20, 778, 161, 112, panel)
        self.rect(195, 778, 161, 112, panel)
        self.text(34, 804, "MEMORY", 12, muted)
        self.text(34, 841, f"{data['mem_used']:.1f} / {data['mem_total']:.0f}", 22, fg, True)
        self.text(34, 867, "GiB USED", 11, muted)
        self.text(209, 804, "VM STATUS", 12, muted)
        for idx, (vmid, status) in enumerate(data["vms"]):
            self.text(209, 833 + idx * 25, f"{vmid}  {status}", 13, (112, 213, 183) if status == "RUNNING" else fg, True)
        self.text(24, 918, f"{data['ip']}  /  UP {data['uptime'] // 3600}H {data['uptime'] % 3600 // 60}M", 15, muted)
        if touch_mode:
            self.text(24, 942, f"SWIPE L/R: VIEW    UP/DOWN: {brightness}%", 9, muted)
        else:
            self.text(24, 942, "DISPLAY ONLY  -  NO TOUCH ACTIONS", 10, muted)
        self.lib.cairo_surface_flush(self.surface)

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
                            gestures.append(("tap", 0))
                    self.down = False
                    self.start = None
                    self.finish = False
        return gestures


def switch_vt(number):
    subprocess.run(["chvt", str(number)], check=True, timeout=3)


def shifted_state(current, horizontal_delta):
    """Swipe left for the next view, right for the previous view."""
    step = 1 if horizontal_delta < 0 else -1
    return SCREEN_STATES[(SCREEN_STATES.index(current) + step) % len(SCREEN_STATES)]


def run_touch(painter):
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
    try:
        line_length = framebuffer_info(fb_fd)
        gesture = TouchGesture()
        state = "panel"
        next_refresh = 0
        with mmap.mmap(fb_fd, line_length * HEIGHT, mmap.MAP_SHARED, mmap.PROT_WRITE) as framebuffer:
            def blit():
                pixels, stride = painter.pixels()
                for y in range(HEIGHT):
                    framebuffer[y * line_length:y * line_length + WIDTH * 4] = pixels[y * stride:y * stride + WIDTH * 4]

            def refresh():
                painter.draw(snapshot(), touch_mode=True, brightness=round(100 * brightness / maximum))
                blit()

            def show_panel():
                # Keep the LCD dark while chvt/fbcon and fb_blank redraw the
                # framebuffer. Reveal it only after our own frame is ready.
                (backlight / "brightness").write_text("0")
                switch_vt(2)
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                time.sleep(0.12)
                refresh()
                (backlight / "brightness").write_text(str(brightness))
                blit()

            def show_console():
                switch_vt(1)
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))

            def show_off():
                # Clear residual LCD pixels before blanking the display.
                painter.rect(0, 0, WIDTH, HEIGHT, (0, 0, 0))
                painter.lib.cairo_surface_flush(painter.surface)
                blit()
                (backlight / "brightness").write_text("0")
                (backlight / "bl_power").write_text("4")
                fb_blank.write_text("4")

            try:
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))
                switch_vt(2)
                time.sleep(0.35)
                refresh()
                next_refresh = time.monotonic() + 15
                while not STOP:
                    timeout = min(1, max(0, next_refresh - time.monotonic()))
                    readable, _, _ = select.select([touch_fd], [], [], timeout)
                    if readable:
                        try:
                            events = gesture.feed(os.read(touch_fd, 24 * 64))
                        except BlockingIOError:
                            events = []
                        for action, value in events:
                            if action == "swipe_vertical":
                                increments = max(1, min(5, round(abs(value) / 100)))
                                brightness = min(maximum, max(minimum, brightness + (step * increments if value < 0 else -step * increments)))
                                if state != "off":
                                    (backlight / "brightness").write_text(str(brightness))
                                if state == "panel":
                                    refresh()
                                print(f"gesture=swipe_vertical brightness={round(100 * brightness / maximum)}% state={state}", flush=True)
                            elif action == "swipe_horizontal":
                                target = shifted_state(state, value)
                                if target == "panel":
                                    show_panel()
                                    next_refresh = time.monotonic() + 15
                                elif target == "console":
                                    show_console()
                                else:
                                    show_off()
                                state = target
                                print(f"gesture=swipe_horizontal direction={'left' if value < 0 else 'right'} state={state}", flush=True)
                            elif action == "tap":
                                print(f"gesture=tap reserved state={state}", flush=True)
                    if state == "panel" and time.monotonic() >= next_refresh:
                        refresh()
                        next_refresh = time.monotonic() + 15
            finally:
                fb_blank.write_text("0")
                (backlight / "bl_power").write_text("0")
                (backlight / "brightness").write_text(str(brightness))
                switch_vt(1)
    finally:
        os.close(fb_fd)
        os.close(touch_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", help="Write a PNG; do not touch the screen")
    parser.add_argument("--once", action="store_true", help="Draw once and exit")
    parser.add_argument("--touch", action="store_true", help="Enable tap/vertical-swipe trial")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    painter = Painter()
    try:
        if args.touch:
            run_touch(painter)
            return
        if args.preview:
            painter.draw(snapshot())
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
                    painter.draw(snapshot())
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
