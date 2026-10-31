"""Independent R1 fan policy: FAN2 storage with CPU assist, FAN3 CPU.

Missing sensors or zero RPM force both channels to full speed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import signal
import time

from r1_hardware import FanCurve, FanPoint
from r1_fan_control_ipc import publish_status, requested_mode
from r1_fan_config import CONFIG_FILE, DEFAULT, curves, digest, load

BASELINE_PWM = 120
EXPECTED_DRIVES = 4
CPU_CURVE, DRIVE_CURVE = curves(DEFAULT)
CPU_ASSIST = FanCurve(tuple(FanPoint(t, p) for t, p in ((80, 120), (85, 160), (90, 220), (95, 255))))
STOP = False


def stop(_signal, _frame):
    global STOP
    STOP = True


def read_int(path: Path) -> int:
    return int(path.read_text().strip())


def named_hwmon(name: str, root: Path = Path("/sys/class/hwmon")) -> list[Path]:
    devices = []
    for candidate in root.glob("hwmon*"):
        try:
            if (candidate / "name").read_text().strip() == name:
                devices.append(candidate)
        except OSError:
            continue
    return devices


def single_hwmon(name: str, root: Path = Path("/sys/class/hwmon")) -> Path:
    matches = named_hwmon(name, root)
    if len(matches) != 1:
        raise RuntimeError(f"expected one {name} hwmon, found {len(matches)}")
    return matches[0]


@dataclass(frozen=True)
class Reading:
    cpu_c: float | None
    drive_c: float | None
    drive_count: int
    fan2: int
    fan3: int
    pwm2: int
    pwm3: int
    mode2: int
    mode3: int


def temperatures(root: Path = Path("/sys/class/hwmon")) -> tuple[float | None, float | None, int]:
    try:
        cpu = single_hwmon("coretemp", root)
        cpu_values = [read_int(path) / 1000 for path in cpu.glob("temp*_input")]
        drives = named_hwmon("drivetemp", root)
        drive_values = [read_int(path / "temp1_input") / 1000 for path in drives]
        if not cpu_values or not drive_values:
            return None, None, len(drives)
        if any(not 0 <= value <= 105 for value in cpu_values):
            return None, None, len(drives)
        if any(not 0 <= value <= 80 for value in drive_values):
            return None, None, len(drives)
        return max(cpu_values), max(drive_values), len(drives)
    except (OSError, ValueError, RuntimeError):
        return None, None, 0


def sample(chip: Path, root: Path = Path("/sys/class/hwmon")) -> Reading:
    cpu, drive, count = temperatures(root)
    return Reading(
        cpu, drive, count,
        read_int(chip / "fan2_input"), read_int(chip / "fan3_input"),
        read_int(chip / "pwm2"), read_int(chip / "pwm3"),
        read_int(chip / "pwm2_enable"), read_int(chip / "pwm3_enable"),
    )


def target_pwms(reading: Reading, expected_drives: int,
                active_curves: tuple[FanCurve, FanCurve] | None = None) -> tuple[int, int]:
    """Return (FAN2 chassis, FAN3 CPU), retaining global fail-high protection."""
    if (reading.cpu_c is None or reading.drive_c is None
            or reading.drive_count < expected_drives
            or reading.fan2 <= 0 or reading.fan3 <= 0):
        return 255, 255
    cpu_curve, drive_curve = active_curves or (CPU_CURVE, DRIVE_CURVE)
    chassis = max(BASELINE_PWM, drive_curve.pwm_at(reading.drive_c),
                  CPU_ASSIST.pwm_at(reading.cpu_c))
    cpu = max(BASELINE_PWM, cpu_curve.pwm_at(reading.cpu_c))
    return chassis, cpu


def target_pwm(reading: Reading, expected_drives: int,
               active_curves: tuple[FanCurve, FanCurve] | None = None) -> int:
    """Compatibility summary; actual writes use independent channel targets."""
    return max(target_pwms(reading, expected_drives, active_curves))


def next_pwm(current: int, requested: int) -> int:
    """Rise immediately, but lower slowly to avoid oscillation near a threshold."""
    if requested > current:
        return requested
    if requested < current:
        return max(requested, current - 5)
    return current


def controllable_mode(mode: int, pwm: int) -> bool:
    """The driver reports mode 0 when a manual channel reaches full duty."""
    return mode == 1 or (mode == 0 and pwm == 255)


def set_channels(chip: Path, pwm2: int, pwm3: int) -> None:
    if not all(BASELINE_PWM <= value <= 255 for value in (pwm2, pwm3)):
        raise ValueError("PWM outside increase-only trial range")
    for channel, value in ((2, pwm2), (3, pwm3)):
        mode = read_int(chip / f"pwm{channel}_enable")
        node = chip / f"pwm{channel}"
        current = read_int(node)
        if not controllable_mode(mode, current):
            raise RuntimeError(f"pwm{channel} has an unexpected control mode")
        if mode == 0 and value < 255:
            (chip / f"pwm{channel}_enable").write_text("1\n")
        if read_int(node) != value:
            node.write_text(f"{value}\n")
        if read_int(node) != value:
            raise RuntimeError(f"pwm{channel} readback mismatch")
        if value < 255 and read_int(chip / f"pwm{channel}_enable") != 1:
            raise RuntimeError(f"pwm{channel} did not return to manual mode")


def set_both(chip: Path, value: int) -> None:
    set_channels(chip, value, value)


def restore(chip: Path) -> None:
    cpu, drive, drive_count = temperatures()
    try:
        fans_running = read_int(chip / "fan2_input") > 0 and read_int(chip / "fan3_input") > 0
    except (OSError, ValueError):
        fans_running = False
    safe_to_restore = (fans_running and drive_count >= EXPECTED_DRIVES
                       and cpu is not None and drive is not None
                       and cpu < 80 and drive < 50)
    value = BASELINE_PWM if safe_to_restore else 255
    set_both(chip, value)
    print(f"restored={value} cpu={cpu} drive={drive}", flush=True)


def run(seconds: int, chip: Path) -> None:
    initial = sample(chip)
    if not all(BASELINE_PWM <= value <= 255 for value in (initial.pwm2, initial.pwm3)):
        raise RuntimeError("PWM is below the verified baseline or out of range")
    if not (controllable_mode(initial.mode2, initial.pwm2)
            and controllable_mode(initial.mode3, initial.pwm3)):
        raise RuntimeError("PWM mode differs from verified manual/full-speed modes")
    deadline = None if seconds == 0 else time.monotonic() + seconds
    applied = (initial.pwm2, initial.pwm3)
    previous_mode = None
    active_curves = (CPU_CURVE, DRIVE_CURVE)
    curve_id = digest(DEFAULT)
    config_mtime = None
    print(f"curve_start={seconds}s drives={initial.drive_count}/{EXPECTED_DRIVES} "
          f"initial_pwm={initial.pwm2}/{initial.pwm3}", flush=True)
    try:
        while not STOP and (deadline is None or time.monotonic() < deadline):
            try:
                mtime = CONFIG_FILE.stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime != config_mtime:
                try:
                    active_record = load(CONFIG_FILE)
                    active_curves = curves(active_record)
                    curve_id = digest(active_record)
                    config_mtime = mtime
                except (OSError, ValueError, KeyError, TypeError) as error:
                    print(f"curve_config_rejected={error}", flush=True)
            reading = sample(chip)
            if not (controllable_mode(reading.mode2, reading.pwm2)
                    and controllable_mode(reading.mode3, reading.pwm3)):
                raise RuntimeError("PWM mode changed during trial")
            mode = requested_mode(clear_expired=True)
            target = (255, 255) if mode == "full" else target_pwms(reading, EXPECTED_DRIVES, active_curves)
            applied = tuple(next_pwm(current, requested) for current, requested in zip(applied, target))
            set_channels(chip, *applied)
            previous_mode = mode
            try:
                publish_status(mode, max(applied), curve_id=curve_id, pwm2=applied[0], pwm3=applied[1])
            except OSError as error:
                print(f"status_write_error={error}", flush=True)
            print(f"cpu={reading.cpu_c} drive={reading.drive_c} "
                  f"fans={reading.fan2}/{reading.fan3} mode={mode} "
                  f"requested={target} applied={applied}", flush=True)
            for _ in range(1 if mode == "full" else 5):
                if STOP or (deadline is not None and time.monotonic() >= deadline):
                    break
                time.sleep(1)
    finally:
        restore(chip)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "run", "restore"))
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    if args.action == "run" and args.seconds != 0 and not 60 <= args.seconds <= 600:
        parser.error("trial duration must be 0 (continuous) or 60-600 seconds")
    board = Path("/sys/class/dmi/id/board_name").read_text().strip()
    if board != "HS-AFS-R1":
        raise RuntimeError(f"unexpected board: {board}")
    chip = single_hwmon("it8613")
    if args.action == "check":
        reading = sample(chip)
        print(f"sample={reading} targets={target_pwms(reading, EXPECTED_DRIVES)}")
    elif args.action == "restore":
        restore(chip)
    else:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        run(args.seconds, chip)


if __name__ == "__main__":
    main()
