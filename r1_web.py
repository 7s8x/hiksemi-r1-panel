#!/usr/bin/env python3
"""Local HTTPS management UI for the R1 panel, fan policy, and light.

The web process never writes the framebuffer or PWM registers. Screen and RGB
commands are serialized by the panel process; fan requests go to its separate
controller. No framebuffer image is written to disk or logged.
"""

from __future__ import annotations

import argparse
import hashlib
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit
import zlib

from r1_fan_config import CONFIG_FILE, DEFAULT, digest, load, save, validate
from r1_fan_control_ipc import control_status, request_mode
from r1_panel_runtime import SnapshotCollector, read_state, request
import r1_panel_repository as panel_repo
from r1_panel_repository import HEIGHT, WIDTH, framebuffer_info
from r1_web_views import drive_models, render_fans, render_information, system_identity
from r1_svg_painter import paint_svg
from r1_web_capacity import storage_capacity
from r1_fan_curve import temperatures as fan_temperatures
from r1_theme import load_theme, save_theme


SELECTABLE_THEMES = ("mocha",)


STATE_DIR = Path("/var/lib/r1-web")
ADMIN_FILE = STATE_DIR / "admin.json"
INITIAL_FILE = STATE_DIR / "initial-password"
CERT_FILE = STATE_DIR / "server.crt"
KEY_FILE = STATE_DIR / "server.key"
RGB_VERIFIED = STATE_DIR / "rgb-verified"
ALLOWED = tuple(ipaddress.ip_network(value) for value in
                ("192.168.50.0/24", "100.64.0.0/10", "127.0.0.1/32"))
HTML = Path(__file__).with_name("r1_web.html")
FAVICON = Path(__file__).with_name("r1_web_favicon.svg")
SESSIONS: dict[str, tuple[float, str]] = {}
FAILURES: dict[str, list[float]] = {}
AUTH_LOCK = threading.Lock()
FRAME_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
THEME_LOCK = threading.Lock()
FRAME_CACHE: tuple[float, str, bytes] | None = None
PUBLIC_COLLECTOR: SnapshotCollector | None = None


def hash_password(password: str, salt: bytes | None = None) -> dict:
    if not 8 <= len(password) <= 256:
        raise ValueError("admin password must contain 8–256 characters")
    salt = salt or secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return {"salt": salt.hex(), "hash": result.hex()}


def verify_password(password: str, record: dict) -> bool:
    try:
        expected = bytes.fromhex(record["hash"])
        salt = bytes.fromhex(record["salt"])
        result = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(result, expected)
    except (ValueError, KeyError, TypeError):
        return False


def atomic_json(path: Path, record: dict):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def change_admin_password(current: str, new: str) -> None:
    if not isinstance(current, str) or not isinstance(new, str):
        raise ValueError("invalid password request")
    if not 8 <= len(new) <= 256:
        raise ValueError("new password must contain 8–256 characters")
    with AUTH_LOCK:
        record = json.loads(ADMIN_FILE.read_text(encoding="utf-8"))
        if not verify_password(current, record):
            raise ValueError("current password is incorrect")
        if hmac.compare_digest(current, new):
            raise ValueError("new password must differ from current password")
        atomic_json(ADMIN_FILE, hash_password(new))
        SESSIONS.clear()


def initialize():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    if not ADMIN_FILE.exists():
        password = secrets.token_urlsafe(24)
        atomic_json(ADMIN_FILE, hash_password(password))
        fd = os.open(INITIAL_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(password + "\n")
        print(f"Initial password stored locally at {INITIAL_FILE}; not printed", flush=True)
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-sha256", "-nodes", "-days", "365", "-subj", "/CN=192.168.50.3",
            "-addext", "subjectAltName=IP:192.168.50.3",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        ], check=True, capture_output=True, timeout=20)
        os.chmod(KEY_FILE, 0o600)
        os.chmod(CERT_FILE, 0o600)
    print("HTTPS certificate SHA-256 fingerprint:", flush=True)
    subprocess.run(["openssl", "x509", "-in", str(CERT_FILE), "-noout", "-fingerprint", "-sha256"],
                   check=True, timeout=5)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def rgb_png(frame: bytes, stride: int) -> bytes:
    if len(frame) < stride * HEIGHT or stride < WIDTH * 4:
        raise ValueError("short framebuffer")
    rows = bytearray()
    for y in range(HEIGHT):
        rows.append(0)
        start = y * stride
        for x in range(WIDTH):
            pixel = start + x * 4
            rows.extend((frame[pixel + 2], frame[pixel + 1], frame[pixel]))
    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", zlib.compress(rows, 3)) + png_chunk(b"IEND", b""))


def capture_frame(state: str) -> bytes:
    global FRAME_CACHE
    with FRAME_LOCK:
        if FRAME_CACHE is not None and FRAME_CACHE[1] == state and time.monotonic() - FRAME_CACHE[0] < 0.7:
            return FRAME_CACHE[2]
        if state == "off":
            raw, stride = bytes(WIDTH * HEIGHT * 4), WIDTH * 4
        else:
            fd = os.open("/dev/fb0", os.O_RDONLY | os.O_CLOEXEC)
            try:
                stride = framebuffer_info(fd)
                raw = os.pread(fd, stride * HEIGHT, 0)
            finally:
                os.close(fd)
        encoded = rgb_png(raw, stride)
        FRAME_CACHE = (time.monotonic(), state, encoded)
        return encoded


def public_snapshot() -> dict:
    """Collect read-only data without exposing disk serials or VM names."""
    total, used = panel_repo.memory()
    try:
        uptime = int(float(panel_repo.read_text("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        uptime = 0
    nvme_temp = panel_repo.temp_for_hwmon("nvme")
    nvme_size, nvme_kind, _ = panel_repo.nvme_identity()
    storage = [((nvme_size, nvme_kind, "****"), nvme_temp)]
    drive_temps = []
    disks = panel_repo.discover_drives()
    pve_version, cpu_model = system_identity()
    for bay, (_, disk) in enumerate(disks, start=1):
        label = f"BAY {bay}"
        if disk is None:
            storage.append(((label, "UNVERIFIED", "--"), None))
            continue
        temperature = panel_repo.drive_temperature(disk)
        size, kind, _ = panel_repo.bay_identity(label, disk)
        storage.append(((size, kind, "****"), temperature))
        drive_temps.append((label, temperature))
    usage = panel_repo.storage_usage(disks)
    capacity = storage_capacity(disks)
    fan_cpu_temp, fan_drive_temp, fan_drive_count = fan_temperatures()
    rgb = None
    try:
        response = request({"action": "rgb_state", "value": None})
        if response.get("ok") and isinstance(response.get("rgb"), list):
            rgb = response["rgb"][0] != 0
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {
        "cpu": panel_repo.temp_for_hwmon("coretemp"),
        "fan_basis": (fan_cpu_temp, fan_drive_temp, fan_drive_count),
        "cpu_usage": panel_repo.cpu_usage(),
        "drives": drive_temps,
        "storage": storage,
        "storage_models": drive_models(disks),
        "storage_usage": usage,
        "storage_capacity": capacity,
        "pve_version": pve_version,
        "cpu_model": cpu_model,
        "mem_total": total,
        "mem_used": used,
        "uptime": uptime,
        "vms": [(f"VM {vmid}", panel_repo.vm_status(vmid)) for vmid in (100, 111)],
        "fans": panel_repo.fan_readings(),
        "control": control_status(),
        "rgb": rgb,
        "captured_at": time.monotonic(),
        "clock": time.strftime("%H:%M"),
    }


def collect_public_frames() -> dict:
    """Render two independent images in memory; never touch the physical display."""
    data = public_snapshot()
    frames = {"panel_svg": render_information(data, web_layout=True),
              "fans_svg": render_fans(data, web_layout=True)}
    for view in ("panel", "fans"):
        painter = panel_repo.Painter()
        try:
            if view == "panel":
                paint_svg(painter, frames["panel_svg"])
            else:
                painter.draw(data, view=view)
            pixels, stride = painter.pixels()
            frames[view] = rgb_png(pixels, stride)
        finally:
            painter.close()
    frames["updated"] = time.time()
    return frames


def client_allowed(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in network for network in ALLOWED)
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "R1PanelWeb/1"

    def log_message(self, format, *args):
        # Deliberately avoid logging URLs, sessions, or console content.
        pass

    def _send(self, code: int, body: bytes, mime: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value: dict, extra: dict | None = None):
        self._send(code, json.dumps(value, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _session(self) -> tuple[str, str] | None:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar["r1session"].value
        except (KeyError, cookies.CookieError):
            return None
        with AUTH_LOCK:
            record = SESSIONS.get(token)
            if record is None or record[0] < time.time():
                SESSIONS.pop(token, None)
                return None
            return token, record[1]

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        return origin == "https://" + self.headers.get("Host", "")

    def _allowed(self) -> bool:
        if not client_allowed(self.client_address[0]):
            self._json(403, {"error": "source denied"})
            return False
        return True

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 4096:
            raise ValueError("invalid body length")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if not self._allowed():
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/favicon.svg":
            self._send(200, FAVICON.read_bytes(), "image/svg+xml")
            return
        if path == "/api/theme":
            self._json(200, {"theme": load_theme(), "choices": SELECTABLE_THEMES})
            return
        if path in ("/public/panel.png", "/public/fans.png",
                    "/public/panel.svg", "/public/fans.svg"):
            frames, _ = PUBLIC_COLLECTOR.latest() if PUBLIC_COLLECTOR else ({}, 0)
            view = "panel" if "/panel." in path else "fans"
            svg = path.endswith(".svg")
            key = view + ("_svg" if svg else "")
            if time.time() - frames.get("updated", 0) > 45 or key not in frames:
                self._json(503, {"error": "public display unavailable"})
                return
            self._send(200, frames[key], "image/svg+xml" if svg else "image/png")
            return
        session = self._session()
        if session is None:
            self._json(401, {"error": "login required"})
            return
        if path == "/api/status":
            panel = read_state()
            try:
                active_curves = load()
                curve_error = None
            except (OSError, ValueError, KeyError, TypeError) as error:
                active_curves = DEFAULT
                curve_error = str(error)[:120]
            self._json(200, {"panel": panel, "fan": control_status(),
                             "curves": active_curves, "curve_id": digest(active_curves),
                             "baseline": DEFAULT,
                             "curve_error": curve_error,
                             "rgb_color_enabled": RGB_VERIFIED.exists(),
                             "csrf": session[1]})
        elif path == "/api/frame.png":
            panel = read_state()
            if panel is None:
                self._json(503, {"error": "panel offline"})
                return
            try:
                self._send(200, capture_frame(panel["state"]), "image/png")
            except (OSError, RuntimeError, ValueError) as error:
                self._json(503, {"error": f"capture unavailable: {error}"})
        elif path == "/api/rgb":
            try:
                response = request({"action": "rgb_state", "value": None})
                self._json(200 if response.get("ok") else 503, response)
            except (OSError, ValueError) as error:
                self._json(503, {"error": str(error)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return
        if not self._origin_ok():
            self._json(403, {"error": "origin denied"})
            return
        path = urlsplit(self.path).path
        try:
            body = self._body()
        except (ValueError, UnicodeError, TypeError):
            self._json(400, {"error": "invalid JSON"})
            return
        if path == "/api/login":
            address = self.client_address[0]
            now = time.time()
            with AUTH_LOCK:
                attempts = [when for when in FAILURES.get(address, []) if now - when < 900]
                FAILURES[address] = attempts
                blocked = len(attempts) >= 5
            if blocked:
                self._json(429, {"error": "too many attempts"})
                return
            with AUTH_LOCK:
                try:
                    record = json.loads(ADMIN_FILE.read_text(encoding="utf-8"))
                    valid = isinstance(body, dict) and isinstance(body.get("password"), str) and verify_password(body["password"], record)
                except (OSError, ValueError):
                    valid = False
                if not valid:
                    FAILURES.setdefault(address, []).append(now)
                else:
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                    FAILURES.pop(address, None)
                    SESSIONS[token] = (now + 8 * 3600, csrf)
            if not valid:
                self._json(401, {"error": "invalid credentials"})
                return
            self._json(200, {"ok": True, "csrf": csrf},
                       {"Set-Cookie": f"r1session={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=28800"})
            return
        session = self._session()
        if session is None:
            self._json(401, {"error": "login required"})
            return
        if not hmac.compare_digest(self.headers.get("X-R1-CSRF", ""), session[1]):
            self._json(403, {"error": "CSRF denied"})
            return
        try:
            if path == "/api/logout":
                with AUTH_LOCK:
                    SESSIONS.pop(session[0], None)
                self._json(200, {"ok": True}, {"Set-Cookie": "r1session=; Max-Age=0; Secure; HttpOnly; SameSite=Strict; Path=/"})
            elif path == "/api/password":
                if not isinstance(body, dict) or set(body) != {"current", "new"}:
                    raise ValueError("invalid password request")
                change_admin_password(body["current"], body["new"])
                self._json(200, {"ok": True}, {"Set-Cookie": "r1session=; Max-Age=0; Secure; HttpOnly; SameSite=Strict; Path=/"})
            elif path == "/api/theme":
                if not isinstance(body, dict) or set(body) != {"theme"}:
                    raise ValueError("invalid theme request")
                if body["theme"] not in SELECTABLE_THEMES:
                    raise ValueError("theme is not selectable")
                with THEME_LOCK:
                    save_theme(body["theme"])
                self._json(200, {"ok": True, "theme": load_theme()})
            elif path == "/api/panel":
                if not isinstance(body, dict) or set(body) != {"action", "value"}:
                    raise ValueError("invalid panel command")
                result = request(body)
                self._json(200 if result.get("ok") else 400, result)
            elif path == "/api/fan/mode":
                if not isinstance(body, dict) or body.get("mode") not in ("auto", "full"):
                    raise ValueError("invalid fan mode")
                if not control_status()["live"]:
                    raise RuntimeError("fan controller offline")
                request_mode(body["mode"])
                self._json(200, {"ok": True, "status": control_status()})
            elif path == "/api/fan/curves":
                with CONFIG_LOCK:
                    before = load()
                    after = validate(body)
                    if not control_status()["live"]:
                        raise RuntimeError("fan controller offline")
                    save(after)
                    wanted = digest(after)
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        current = control_status()
                        if current["live"] and current.get("curve_id") == wanted:
                            self._json(200, {"ok": True, "curves": after, "curve_id": wanted})
                            return
                        time.sleep(0.2)
                    save(before)
                    raise RuntimeError("fan controller did not acknowledge curve; previous curve restored")
            elif path == "/api/rgb":
                if not isinstance(body, dict) or set(body) != {"mode", "red", "green", "blue"}:
                    raise ValueError("invalid RGB request")
                result = request({"action": "rgb", "value": [body[name] for name in ("mode", "red", "green", "blue")]})
                self._json(200 if result.get("ok") else 400, result)
            else:
                self._json(404, {"error": "not found"})
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            self._json(400, {"error": str(error)[:160]})


def main():
    global PUBLIC_COLLECTOR
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("init", "serve"))
    parser.add_argument("--host", default="192.168.50.3")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    if args.action == "init":
        initialize()
        return
    if not all(path.is_file() for path in (ADMIN_FILE, CERT_FILE, KEY_FILE)):
        raise RuntimeError("run init before serving")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    PUBLIC_COLLECTOR = SnapshotCollector(collect_public_frames, interval=15)
    PUBLIC_COLLECTOR.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        PUBLIC_COLLECTOR.stop()
        server.server_close()


if __name__ == "__main__":
    main()
