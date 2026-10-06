"""Validated, atomically stored R1 fan curves.

The controller owns PWM writes. Other processes may only replace this file;
invalid or unreadable updates must never lower the last good fan target.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import time

from r1_hardware import FanCurve, FanPoint


CONFIG_FILE = Path("/var/lib/r1-fan-curve/curves.json")
DEFAULT = {
    "version": 1,
    "cpu": [[55, 120], [65, 120], [70, 140], [80, 180], [90, 220], [95, 255]],
    "drive": [[35, 120], [40, 120], [45, 140], [50, 180], [55, 220], [60, 255]],
}


def validate(record: object) -> dict:
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
        baseline = FanCurve(tuple(FanPoint(*point) for point in DEFAULT[name]))
        proposed = FanCurve(tuple(FanPoint(*point) for point in normalized))
        if any(proposed.pwm_at(temp) < baseline.pwm_at(temp)
               for temp in range(low, high + 1)):
            raise ValueError(f"{name} cannot be slower than the verified baseline")
        result[name] = normalized
    return result


def curves(record: dict) -> tuple[FanCurve, FanCurve]:
    validated = validate(record)
    return tuple(FanCurve(tuple(FanPoint(*point) for point in validated[name]))
                 for name in ("cpu", "drive"))


def digest(record: dict) -> str:
    encoded = json.dumps(validate(record), sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:16]


def load(path: Path = CONFIG_FILE) -> dict:
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return validate(DEFAULT)


def save(record: object, path: Path = CONFIG_FILE) -> dict:
    validated = validate(record)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if load(path) != validated:
            raise RuntimeError("curve readback mismatch")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return validated
