"""Small root-only mode request and status interface for the R1 fan service.

Modes are ``high`` / ``low`` (persistent curve profiles) and ``full`` (a bounded
60-second burst).  A full-speed request records the profile it must return to,
so expiry restores the operator's last choice instead of a hard-coded default.
The historical ``auto`` spelling is accepted as an alias for ``high``.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time


MODE_FILE = Path("/var/lib/r1-fan-curve/mode")
STATUS_FILE = Path("/run/r1-fan-curve/status.json")
PROFILES = frozenset(("high", "low"))
MODES = frozenset(("high", "low", "full"))
ALIASES = {"auto": "high"}
FULL_SPEED_SECONDS = 60


def normalize_mode(mode: str) -> str:
    return ALIASES.get(mode, mode)


def _replace_text(path: Path, contents: str) -> None:
    """Atomically replace a short control/status file in an existing directory."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _stored_request(path: Path) -> tuple[str, float | None, str]:
    """Return (mode, full-speed expiry, profile to restore)."""
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return "high", None, "high"
    except (OSError, UnicodeError):
        return "full", None, "high"  # fail high on an unreadable request
    if value in ("high", "low", "auto"):
        mode = normalize_mode(value)
        return mode, None, mode
    try:
        record = json.loads(value)
        expiry = record["expires_at"]
        if (record["mode"] == "full" and isinstance(expiry, (int, float))
                and math.isfinite(expiry)):
            back = normalize_mode(record.get("return_to", "high"))
            if back not in PROFILES:
                back = "high"
            return "full", expiry, back
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return "full", None, "high"  # invalid requests never lower fan speed


def requested_mode(path: Path = MODE_FILE, clear_expired: bool = False) -> str:
    mode, expiry, back = _stored_request(path)
    if mode == "full" and expiry is not None and time.time() >= expiry:
        if clear_expired:
            request_mode(back, path)
        return back
    return mode


def request_mode(mode: str, path: Path = MODE_FILE) -> None:
    mode = normalize_mode(mode)
    if mode not in MODES:
        raise ValueError(f"unsupported fan mode: {mode}")
    if mode == "full":
        stored_mode, expiry, back = _stored_request(path)
        if stored_mode == "full" and expiry is not None and expiry > time.time():
            return  # repeated taps do not extend the original 60-second burst
        origin = back if stored_mode == "full" else (
            stored_mode if stored_mode in PROFILES else "high")
        content = json.dumps({"mode": "full", "expires_at": time.time() + FULL_SPEED_SECONDS,
                              "return_to": origin})
    else:
        content = mode + "\n"
    _replace_text(path, content)


def publish_status(mode: str, pwm: int, path: Path = STATUS_FILE,
                   curve_id: str | None = None,
                   pwm2: int | None = None, pwm3: int | None = None) -> None:
    mode = normalize_mode(mode)
    if mode not in MODES or not 120 <= pwm <= 255:
        raise ValueError("invalid fan status")
    record = {"mode": mode, "pwm": pwm, "updated": time.time()}
    if pwm2 is not None or pwm3 is not None:
        if not all(type(value) is int and 120 <= value <= 255 for value in (pwm2, pwm3)):
            raise ValueError("invalid per-channel fan status")
        record.update(pwm2=pwm2, pwm3=pwm3)
    if curve_id is not None:
        record["curve_id"] = curve_id
    _replace_text(path, json.dumps(record, separators=(",", ":")))


def control_status(mode_path: Path = MODE_FILE, status_path: Path = STATUS_FILE,
                   max_age: float = 15.0) -> dict:
    desired = requested_mode(mode_path)
    stored_mode, expiry, back = _stored_request(mode_path)
    remaining = (max(0, math.ceil(expiry - time.time()))
                 if stored_mode == "full" and expiry is not None and desired == "full"
                 else None)
    result = {"desired": desired, "mode": None, "pwm": None, "curve_id": None,
              "live": False, "remaining": remaining, "return_to": back}
    try:
        status = json.loads(status_path.read_text(encoding="ascii"))
        mode = normalize_mode(status["mode"])
        pwm = status["pwm"]
        updated = status["updated"]
        if (mode in MODES and isinstance(pwm, int) and 120 <= pwm <= 255
                and isinstance(updated, (int, float))
                and 0 <= time.time() - updated <= max_age):
            result.update(mode=mode, pwm=pwm, live=True,
                          curve_id=status.get("curve_id"))
            for channel in (2, 3):
                value = status.get(f"pwm{channel}", pwm)
                if type(value) is int and 120 <= value <= 255:
                    result[f"pwm{channel}"] = value
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, AttributeError):
        pass
    return result
