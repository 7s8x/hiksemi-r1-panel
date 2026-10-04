"""PVE-side R1 hardware discovery and control policy.

This module deliberately performs no hardware writes.  Hardware transports
will be added only after the actual R1 PWM and I2C mappings are verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


IT87_NAMES = frozenset({"it8628", "it8613", "it87"})
MIN_REASONABLE_TEMP = -10.0
MAX_REASONABLE_TEMP = 110.0


@dataclass(frozen=True)
class FanPoint:
    celsius: float
    pwm: int


@dataclass(frozen=True)
class FanCurve:
    """Monotone temperature/PWM policy; it does not imply a safe fan minimum."""

    points: tuple[FanPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("a fan curve needs at least two points")
        for point in self.points:
            if not (MIN_REASONABLE_TEMP <= point.celsius <= MAX_REASONABLE_TEMP):
                raise ValueError("temperature outside plausible sensor range")
            if not (1 <= point.pwm <= 255):
                raise ValueError("PWM must be in the range 1..255")
        for before, after in zip(self.points, self.points[1:]):
            if after.celsius <= before.celsius:
                raise ValueError("temperatures must increase strictly")
            if after.pwm < before.pwm:
                raise ValueError("PWM must not decrease as temperature rises")

    def pwm_at(self, celsius: float | None) -> int:
        """Fail high on a missing, corrupt, or dangerously hot sensor value."""
        if celsius is None or not (MIN_REASONABLE_TEMP <= celsius <= MAX_REASONABLE_TEMP):
            return 255
        if celsius >= self.points[-1].celsius:
            return 255
        if celsius <= self.points[0].celsius:
            return self.points[0].pwm
        for low, high in zip(self.points, self.points[1:]):
            if celsius <= high.celsius:
                fraction = (celsius - low.celsius) / (high.celsius - low.celsius)
                return round(low.pwm + fraction * (high.pwm - low.pwm))
        return 255


@dataclass(frozen=True)
class HwmonSnapshot:
    name: str
    path: str
    fans_rpm: dict[str, int | None]
    pwm: dict[str, int | None]
    pwm_enable: dict[str, int | None]


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_name(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_numbered_files(directory: Path, pattern: str) -> dict[str, int | None]:
    return {path.name: _read_int(path) for path in sorted(directory.glob(pattern))}


def discover_hwmon(hwmon_root: Path = Path("/sys/class/hwmon")) -> list[HwmonSnapshot]:
    """Read sensors and PWM exposure without changing their control mode."""
    snapshots = []
    for directory in sorted(hwmon_root.glob("hwmon*")):
        name = _read_name(directory / "name")
        if not name:
            continue
        snapshots.append(
            HwmonSnapshot(
                name=name,
                path=str(directory),
                fans_rpm=_read_numbered_files(directory, "fan*_input"),
                pwm={
                    path.name: _read_int(path)
                    for path in sorted(directory.glob("pwm[0-9]*"))
                    if path.name.removeprefix("pwm").isdigit()
                },
                pwm_enable=_read_numbered_files(directory, "pwm[0-9]*_enable"),
            )
        )
    return snapshots


def discover_i2c_adapters(
    i2c_root: Path = Path("/sys/bus/i2c/devices"),
) -> dict[int, str]:
    """List adapter names only; do not scan device addresses."""
    adapters = {}
    for path in i2c_root.glob("i2c-[0-9]*"):
        try:
            number = int(path.name.removeprefix("i2c-"))
        except ValueError:
            continue
        name = _read_name(path / "name")
        if name:
            adapters[number] = name
    return dict(sorted(adapters.items()))


def require_verified_channels(
    devices: Iterable[HwmonSnapshot], required: Iterable[str]
) -> HwmonSnapshot:
    """Find exactly one plausible IT87 sensor with the requested fan nodes."""
    required_names = tuple(required)
    matches = [
        device
        for device in devices
        if device.name.lower() in IT87_NAMES
        and all(node in device.fans_rpm for node in required_names)
    ]
    if len(matches) != 1:
        raise RuntimeError("IT87 fan mapping is absent or ambiguous")
    return matches[0]


@dataclass(frozen=True)
class RgbEffect:
    mode: str
    red: int = 0
    green: int = 0
    blue: int = 0

    def __post_init__(self) -> None:
        if self.mode not in {"off", "solid", "breathing", "rainbow"}:
            raise ValueError("unsupported RGB mode")
        if any(not 0 <= channel <= 255 for channel in (self.red, self.green, self.blue)):
            raise ValueError("RGB channels must be in the range 0..255")


def candidate_rgb_register_plan(effect: RgbEffect) -> tuple[tuple[int, int], ...]:
    """Encode a candidate R1 I2C protocol, without selecting a bus or doing I/O.

    The address 0x26 and registers 0x90/0x92..0x94 came from static package
    inspection.  They must be independently verified on this physical host
    before a transport capable of writing I2C is added.
    """
    if effect.mode == "off":
        return ((0x90, 0),)
    if effect.mode == "rainbow":
        return ((0x90, 2),)
    mode_value = 1 if effect.mode == "solid" else 3
    return (
        (0x92, effect.red),
        (0x93, effect.green),
        (0x94, effect.blue),
        (0x90, mode_value),
    )
