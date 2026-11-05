#!/usr/bin/env python3
"""Local management UI for the R1 panel, fan policy, and light.

Serves plain HTTP by default (bound to all interfaces) so it works on any LAN
subnet out of the box. HTTPS is opt-in via --tls for users who terminate TLS
locally instead of behind a reverse proxy.

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
import re
import os
from pathlib import Path
import secrets
import socket
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
from r1_web_views import render_fans, render_information
from r1_collection import read_snapshot
from r1_svg_painter import paint_svg
from r1_theme import load_theme, save_theme


SELECTABLE_THEMES = ("mocha", "latte")
PUBLIC_READONLY = os.environ.get("R1_WEB_PUBLIC", "1").strip().lower() not in ("0", "off", "false", "no")


STATE_DIR = Path("/var/lib/r1-web")
ADMIN_FILE = STATE_DIR / "admin.json"
INITIAL_FILE = STATE_DIR / "initial-password"
CERT_FILE = STATE_DIR / "server.crt"
KEY_FILE = STATE_DIR / "server.key"
RGB_VERIFIED = STATE_DIR / "rgb-verified"
def _load_allowed() -> tuple:
    """Source networks allowed to reach the web service.

    Defaults to loopback plus all RFC1918 private ranges and CGNAT so the
    panel works on any LAN subnet out of the box, regardless of the host's
    actual IP. Override with the R1_WEB_ALLOWED environment variable
    (comma-separated CIDRs), e.g. "192.168.1.0/24,10.0.0.0/8".
    """
    raw = os.environ.get("R1_WEB_ALLOWED")
    values = [v.strip() for v in raw.split(",") if v.strip()] if raw else (
        "127.0.0.1/32", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"
    )
    try:
        return tuple(ipaddress.ip_network(v) for v in values)
    except ValueError as error:
        raise RuntimeError(f"invalid R1_WEB_ALLOWED entry: {error}") from error


ALLOWED = _load_allowed()


def _load_trusted_proxies() -> tuple:
    """Forwarding headers are trusted only from explicitly configured peers."""
    values = os.environ.get("R1_WEB_TRUSTED_PROXIES", "").split(",")
    try:
        return tuple(ipaddress.ip_network(value.strip()) for value in values if value.strip())
    except ValueError as error:
        raise RuntimeError(f"invalid R1_WEB_TRUSTED_PROXIES entry: {error}") from error


TRUSTED_PROXIES = _load_trusted_proxies()
# --- Local access log (configurable, privacy-aware) ---
# The web service deliberately avoids logging request bodies, session cookies,
# or framebuffer content. When enabled (the default), it writes a small access
# log with query strings stripped and sensitive endpoints redacted. Disable it
# with R1_WEB_LOG=0 (also accepts off/false/no/disable/disabled).
_LOG_FLAG = os.environ.get("R1_WEB_LOG", "1").strip().lower()
LOG_ENABLED = _LOG_FLAG not in ("0", "off", "false", "no", "disable", "disabled")
WEB_LOG_FILE = STATE_DIR / "access.log"
_WEB_LOG_MAX_BYTES = 1_000_000
_WEB_LOG_BACKUPS = 3
_WEB_LOG_LOCK = threading.Lock()
_SENSITIVE_PREFIXES = ("/api/login", "/api/password", "/api/session")


def _escape_log_text(value: str) -> str:
    """Keep each event on one line without terminal control characters."""
    return "".join(f"\\x{ord(char):02x}" if ord(char) < 32 or ord(char) == 127 else char
                   for char in str(value))


def _sanitize_access_path(path: str) -> str:
    """Strip userinfo, query and fragment; redact authentication paths."""
    try:
        path = urlsplit(path or "").path
    except ValueError:
        return "<redacted>"
    if any(path.startswith(prefix) for prefix in _SENSITIVE_PREFIXES):
        return "<redacted>"
    return _escape_log_text(path)


def _web_log(format: str, *args) -> None:
    """Append one line to the local access log. Never raises."""
    if not LOG_ENABLED:
        return
    try:
        message = format % args if args else format
    except Exception:
        message = str(format)
    line = time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + _escape_log_text(message) + "\n"
    try:
        with _WEB_LOG_LOCK:
            if WEB_LOG_FILE.exists() and WEB_LOG_FILE.stat().st_size > _WEB_LOG_MAX_BYTES:
                for i in range(_WEB_LOG_BACKUPS - 1, 0, -1):
                    src = WEB_LOG_FILE.with_suffix(f".log.{i}")
                    dst = WEB_LOG_FILE.with_suffix(f".log.{i + 1}")
                    if src.exists():
                        src.replace(dst)
                WEB_LOG_FILE.replace(WEB_LOG_FILE.with_suffix(".log.1"))
            WEB_LOG_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(WEB_LOG_FILE, flags, 0o600), "a", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(line)
    except OSError:
        pass

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
        try:
            INITIAL_FILE.unlink(missing_ok=True)
        except OSError:
            # The password change is already committed. Do not report it as failed.
            _web_log("operation_error initial_password_cleanup")


def initialize(*, tls=False, tls_name="localhost"):
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    theme_file = STATE_DIR / "theme.json"
    if not theme_file.exists():
        save_theme("mocha", path=theme_file)
    if not ADMIN_FILE.exists():
        password = secrets.token_urlsafe(24)
        atomic_json(ADMIN_FILE, hash_password(password))
        fd = os.open(INITIAL_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(password + "\n")
        print(f"Initial password stored locally at {INITIAL_FILE}; not printed", flush=True)
    if not tls:
        return
    try:
        address = ipaddress.ip_address(tls_name)
        san = f"IP:{address}"
    except ValueError:
        import re
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", tls_name):
            raise ValueError("invalid certificate DNS name")
        san = f"DNS:{tls_name}"
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-sha256", "-nodes", "-days", "365", "-subj", f"/CN={tls_name}",
            "-addext", f"subjectAltName={san}",
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
    rows = bytearray((WIDTH * 3 + 1) * HEIGHT)
    for y in range(HEIGHT):
        start = y * stride
        row = frame[start:start + WIDTH * 4]
        offset = y * (WIDTH * 3 + 1) + 1
        rows[offset:offset + WIDTH * 3:3] = row[2::4]
        rows[offset + 1:offset + WIDTH * 3:3] = row[1::4]
        rows[offset + 2:offset + WIDTH * 3:3] = row[0::4]
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
    """Project only display fields; never expose the raw collector document."""
    source = read_snapshot()
    allowed = ('cpu', 'cpu_usage', 'nvme', 'drives', 'storage_models', 'storage_usage',
               'storage_capacity', 'bay_states', 'pve_version', 'cpu_model', 'mem_total',
               'mem_used', 'uptime', 'fans', 'fan_basis', 'captured_at', 'freshness')
    data = {k: source[k] for k in allowed if k in source}
    data['storage'] = [((str(identity[0]), str(identity[1]), '****'), temp)
                       for identity, temp in source.get('storage', [])]
    data['vms'] = [(name if re.fullmatch(r'VM [0-9]+', str(name)) else 'VM --', status)
                   for name, status in source.get('vms', [])]
    data['clock'] = time.strftime('%H:%M')
    data['control'] = control_status()
    data['rgb'] = None
    try:
        response = request({'action': 'rgb_state', 'value': None})
        if response.get('ok') and isinstance(response.get('rgb'), list):
            data['rgb'] = response['rgb'][0] != 0
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return data


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


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound active sockets, including slow TLS handshakes, before creating workers."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, context=None, max_connections=32):
        self.context = context
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            if self.context is not None:
                request = self.context.wrap_socket(request, server_side=True)
            super().process_request_thread(request, client_address)
        except (OSError, ValueError):
            self.shutdown_request(request)
            _web_log("operation_error connection")
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Avoid exception text or request data in the system journal.
        _web_log("operation_error request")


class Handler(BaseHTTPRequestHandler):
    server_version = "R1PanelWeb/1"
    protocol_version = "HTTP/1.1"
    timeout = 15

    def handle_expect_100(self):
        self.send_error(417, "expectation not supported")
        return False

    def parse_request(self):
        if not super().parse_request():
            return False
        lengths = self.headers.get_all("Content-Length", [])
        hosts = self.headers.get_all("Host", [])
        if (len(hosts) != 1 or len(lengths) > 1 or
                self.headers.get_all("Transfer-Encoding", [])):
            self.send_error(400, "invalid request framing")
            return False
        if lengths:
            length = lengths[0]
            if not length.isascii() or not length.isdecimal() or len(length) > 5:
                self.send_error(400, "invalid body length")
                return False
            if int(length) > 4096:
                self.send_error(413, "body too large")
                return False
            if self.command != "POST" and int(length) != 0:
                self.send_error(400, "unexpected request body")
                return False
        return True

    def log_request(self, code="-", size="-"):
        if not LOG_ENABLED:
            return
        path = _sanitize_access_path(getattr(self, "path", ""))
        _web_log("%s %s %s %s", self.address_string(), getattr(self, "command", None) or "-", path, code)

    def log_message(self, format, *args):
        # http.server error strings can contain the entire raw request line.
        # Log only structured, sanitized metadata, never format or its arguments.
        if LOG_ENABLED:
            _web_log("error %s %s", self.address_string(),
                     _sanitize_access_path(getattr(self, "path", "")))

    def _send(self, code: int, body: bytes, mime: str, extra: dict | None = None):
        if code >= 400:
            # Rejected POSTs may have an unread body; never reuse that socket.
            self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
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

    def _trusted_proxy(self) -> bool:
        try:
            peer = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(peer in network for network in TRUSTED_PROXIES)

    def _request_scheme(self) -> str:
        """Scheme of the request as seen by the client.

        Inspects the live socket, and honors X-Forwarded-Proto only from an
        explicitly trusted proxy when the panel
        sits behind a reverse proxy that terminates TLS (the proxy, not this
        process, owns the HTTPS connection).
        """
        if isinstance(self.connection, ssl.SSLSocket):
            return "https"
        proto = ((self.headers.get("X-Forwarded-Proto") or "").lower()
                 if self._trusted_proxy() else "")
        if proto in ("https", "http"):
            return proto
        return "http"

    def _request_host(self) -> str:
        """Host as seen by the client, preferring an upstream-set forwarded host."""
        forwarded = self.headers.get("X-Forwarded-Host") if self._trusted_proxy() else None
        return forwarded or self.headers.get("Host", "")

    def _session_cookie(self, value: str, max_age: int | None = 28800) -> str:
        """Build the session Set-Cookie, only flagging Secure over HTTPS.

        Over plain HTTP a Secure cookie would never be sent by the browser,
        which would silently break login; only set it when the client actually
        reached us over TLS (directly or via a forwarded proto).
        """
        secure = "Secure; " if self._request_scheme() == "https" else ""
        if value == "" or max_age == 0:
            return "r1session=; Max-Age=0; " + secure + "HttpOnly; SameSite=Strict; Path=/"
        return f"r1session={value}; HttpOnly; {secure}SameSite=Strict; Path=/; Max-Age={max_age}"

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        return origin == self._request_scheme() + "://" + self._request_host()

    def _allowed(self) -> bool:
        if not client_allowed(self.client_address[0]):
            self._json(403, {"error": "source denied"})
            return False
        if not self._host_ok():
            self._json(403, {"error": "host denied"})
            return False
        return True

    def _host_ok(self) -> bool:
        authority = self._request_host()
        if any(c in authority for c in "\\/@#?\r\n\t "):
            return False
        try:
            parsed = urlsplit("//" + authority)
            host = parsed.hostname
            port = parsed.port
            if not host or port == 0:
                return False
            # Deployment domain policy belongs to the reverse proxy.
            # Keep authority syntax checks without a domain allowlist.
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 4096:
            raise ValueError("invalid body length")
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise ValueError("incomplete body")
        return json.loads(payload.decode("utf-8"))

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
            if not PUBLIC_READONLY and self._session() is None:
                self._json(401, {"error": "login required"})
                return
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
                curve_error = "curve configuration unavailable"
                _web_log("operation_error curve_read")
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
                _web_log("operation_error capture")
                self._json(503, {"error": "capture unavailable"})
        elif path == "/api/rgb":
            try:
                response = request({"action": "rgb_state", "value": None})
                self._json(200, response) if response.get("ok") else self._json(503, {"error": "RGB unavailable"})
            except (OSError, ValueError) as error:
                _web_log("operation_error rgb_read")
                self._json(503, {"error": "RGB unavailable"})
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
        except (TimeoutError, OSError):
            self._json(408, {"error": "request timeout"})
            return
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
                       {"Set-Cookie": self._session_cookie(token)})
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
                self._json(200, {"ok": True}, {"Set-Cookie": self._session_cookie("", max_age=0)})
            elif path == "/api/password":
                if not isinstance(body, dict) or set(body) != {"current", "new"}:
                    raise ValueError("invalid password request")
                change_admin_password(body["current"], body["new"])
                self._json(200, {"ok": True}, {"Set-Cookie": self._session_cookie("", max_age=0)})
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
                self._json(200, result) if result.get("ok") else self._json(400, {"error": "panel command rejected"})
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
                self._json(200, result) if result.get("ok") else self._json(400, {"error": "RGB command rejected"})
            else:
                self._json(404, {"error": "not found"})
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            _web_log("operation_error command")
            self._json(400, {"error": "command rejected or unavailable"})


def main():
    global PUBLIC_COLLECTOR
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("init", "serve"))
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address; 0.0.0.0 listens on all interfaces so the panel "
                             "is reachable via whatever LAN IP the host happens to have")
    parser.add_argument("--port", type=int, default=8080, help="TCP port to serve on")
    parser.add_argument("--tls", action="store_true",
                        help="serve over HTTPS using the init-generated or a supplied cert/key")
    parser.add_argument("--tls-name", default="localhost", help="certificate IP or DNS name for init --tls")
    parser.add_argument("--cert", default=None, help="TLS certificate path (used with --tls)")
    parser.add_argument("--key", default=None, help="TLS private key path (used with --tls)")
    args = parser.parse_args()
    if args.action == "init":
        initialize(tls=args.tls, tls_name=args.tls_name)
        return
    if not ADMIN_FILE.is_file():
        raise RuntimeError("run init before serving")
    context = None
    if args.tls:
        cert, key = Path(args.cert or CERT_FILE), Path(args.key or KEY_FILE)
        if not all(path.is_file() for path in (cert, key)):
            raise RuntimeError("TLS certificate/key missing; run init or supply --cert and --key")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
    server = BoundedHTTPServer((args.host, args.port), Handler, context=context)
    PUBLIC_COLLECTOR = SnapshotCollector(collect_public_frames, interval=15)
    PUBLIC_COLLECTOR.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        PUBLIC_COLLECTOR.stop()
        server.server_close()


if __name__ == "__main__":
    main()

