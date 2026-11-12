"""Validated, atomically stored R1 fan curves.

The controller owns PWM writes. Other processes may only replace these files;
invalid or unreadable updates must never lower the last good fan target.

Two profiles back the high/low speed modes.  ``high`` keeps the verified
baseline as a floor; ``low`` may be gentler than that baseline but still cannot
go below the 120 PWM hardware floor, must stay monotonic and must reach full
speed by the emergency temperature.  Sensor faults still force 255 in the
controller, independent of either profile.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import time

from r1_hardware import FanCurve, FanPoint


CONFIG_FILE = Path("/var/lib/r1-fan-curve/curves.json")
LOW_CONFIG_FILE = CONFIG_FILE.with_name("curves-low.json")
PROFILES = ("high", "low")
DEFAULT = {
    "version": 1,
    "cpu": [[55, 120], [65, 120], [70, 140], [80, 180], [90, 220], [95, 255]],
    "drive": [[35, 120], [40, 120], [45, 140], [50, 180], [55, 220], [60, 255]],
}


def _profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValueError(f"unsupported fan profile: {profile}")
    return profile


def config_path(profile: str = "high") -> Path:
    """The high profile keeps the historical filename so upgrades are seamless."""
    return CONFIG_FILE if _profile(profile) == "high" else LOW_CONFIG_FILE


def validate(record: object, profile: str = "high") -> dict:
    profile = _profile(profile)
    if (not isinstance(record, dict) or set(record) != {"version", "cpu", "drive"}
            or type(record["version"]) is not int or record["version"] != 1):
        raise ValueError("unsupported fan curve schema")
    result = {"version": 1}
    for name, low, high, emergency in (("cpu", 20, 100, 95), ("drive", 20, 65, 60)):
        points = record[name]
        if not isinstance(points, list) or not 2 <= len(points) <= 12:
            raise ValueError(f"{name} requires 2-12 points")
        previous_temp = low - 1
        previous_pwm = 120
        normalized = []
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) is not int for value in point)):
                raise ValueError(f"invalid {name} point")
            temp, pwm = point
            if not low <= temp <= high or temp <= previous_temp or not 120 <= pwm <= 255 or pwm < previous_pwm:
                raise ValueError(f"unsafe {name} point")
            normalized.append([temp, pwm])
            previous_temp, previous_pwm = temp, pwm
        if normalized[-1][0] > emergency or normalized[-1][1] != 255:
            raise ValueError(f"{name} must reach full speed by {emergency}C")
        if profile == "high":
            baseline = FanCurve(tuple(FanPoint(*point) for point in DEFAULT[name]))
            proposed = FanCurve(tuple(FanPoint(*point) for point in normalized))
            if any(proposed.pwm_at(temp) < baseline.pwm_at(temp)
                   for temp in range(low, high + 1)):
                raise ValueError(f"{name} cannot be slower than the verified baseline")
        result[name] = normalized
    return result


def curves(record: dict, profile: str = "high") -> tuple[FanCurve, FanCurve]:
    validated = validate(record, profile)
    return tuple(FanCurve(tuple(FanPoint(*point) for point in validated[name]))
                 for name in ("cpu", "drive"))


def digest(record: dict, profile: str = "high") -> str:
    encoded = json.dumps(validate(record, profile), sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:16]


def load(path: Path | None = None, *, profile: str = "high") -> dict:
    target = Path(path) if path is not None else config_path(profile)
    try:
        return validate(json.loads(target.read_text(encoding="utf-8")), profile)
    except FileNotFoundError:
        return validate(DEFAULT, profile)


def save(record: object, path: Path | None = None, *, profile: str = "high") -> dict:
    target = Path(path) if path is not None else config_path(profile)
    validated = validate(record, profile)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if load(target, profile=profile) != validated:
            raise RuntimeError("curve readback mismatch")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return validated


def bundle_digest() -> str:
    """Acknowledge an edit to either profile, so Web never waits on a stale id."""
    parts = [f"{name}:{digest(load(profile=name), name)}" for name in PROFILES]
    return hashlib.sha256("|".join(parts).encode("ascii")).hexdigest()[:16]
