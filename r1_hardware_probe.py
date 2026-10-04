"""Read-only hardware inventory for the physical R1 PVE host."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from r1_hardware import discover_hwmon, discover_i2c_adapters


def main() -> None:
    board_path = Path("/sys/class/dmi/id/board_name")
    board_name = board_path.read_text().strip() if board_path.exists() else "unknown"
    print(
        json.dumps(
            {
                "board_name": board_name,
                "hwmon": [asdict(device) for device in discover_hwmon()],
                "i2c_adapters": discover_i2c_adapters(),
                "note": "read-only inventory; PWM channels and RGB address are not verified",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
