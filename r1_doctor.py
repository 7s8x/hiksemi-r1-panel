"""Read-only preflight. Never loads a module or writes to hardware."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from r1_site_config import SITE, bay_states, discover_bays


def check_module(module, expected_sha, *, kernel=None):
    """Validate only a user-supplied module; never insert it."""
    if not module or not expected_sha or len(expected_sha) != 64:
        return False
    try:
        if not Path(module).is_absolute():
            return False
        if hashlib.sha256(Path(module).read_bytes()).hexdigest() != expected_sha:
            return False
        release = kernel or os.uname().release
        result = subprocess.run(["modinfo", "-F", "vermagic", module], capture_output=True,
                                text=True, check=True, timeout=3)
        return result.stdout.split()[0] == release
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


def report():
    results = {}
    try:
        results["board"] = Path("/sys/class/dmi/id/board_name").read_text().strip() == "HS-AFS-R1"
    except OSError:
        results["board"] = False
    results["framebuffer"] = Path("/dev/fb0").exists()
    results["backlight"] = Path("/sys/class/backlight/intel_backlight/max_brightness").exists()
    try:
        text = Path("/proc/bus/input/devices").read_text()
        results["touch"] = "CST0836:00 0911:5288" in text
    except OSError:
        results["touch"] = False
    for command in ("pvesh", "pvesm", "qm", "chvt", "modinfo"):
        results["command_" + command] = shutil.which(command) is not None
    results["cairo"] = __import__("ctypes.util", fromlist=["find_library"]).find_library("cairo") is not None
    results["fontconfig"] = __import__("ctypes.util", fromlist=["find_library"]).find_library("fontconfig") is not None
    results["font"] = Path(__file__).with_name("assets").joinpath("fonts/NotoSansSC-VF.ttf").is_file()
    results["bay_states"] = list(bay_states(discover_bays()))
    results["bay_configuration"] = not any(s in ("missing", "unexpected") for s in results["bay_states"])
    from r1_fan_curve import named_hwmon, temperatures
    results["fan_sensor"] = len(named_hwmon("it8613")) == 1
    cpu, drive, count = temperatures()
    results["temperature_sources"] = cpu is not None and count == len(SITE.occupied_bays) and (count == 0 or drive is not None)
    results["configured_driver"] = check_module(os.environ.get("R1_IT87_MODULE"),
                                                os.environ.get("R1_IT87_SHA256"))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-driver", action="store_true")
    args = parser.parse_args()
    if args.check_driver:
        return 0 if check_module(os.environ.get("R1_IT87_MODULE"), os.environ.get("R1_IT87_SHA256")) else 1
    data = report()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if all(value is True for key, value in data.items() if key != "bay_states") else 1


if __name__ == "__main__":
    raise SystemExit(main())
