"""Narrow, read-only probe of the R1 Toolbox RGB I2C candidate at 0x26.

This deliberately does not scan addresses, force a busy slave, or write a
register value. Reading SMBus byte-data sends a register selector as part of
the read transaction; that is not a proof of device identity.
"""

from __future__ import annotations

import argparse
import os

from r1_rgb_io import (I2C_SLAVE, I2C_SMBUS, I2C_SMBUS_READ, I2C_SMBUS_BYTE_DATA,
                       RGB_ADDRESS, REGISTERS, SmbusData, SmbusIoctlData,
                       ioctl_checked, read_byte_data)


def probe_bus(number: int) -> None:
    path = f"/dev/i2c-{number}"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            ioctl_checked(fd, I2C_SLAVE, RGB_ADDRESS)
            readings = {f"0x{reg:02x}": read_byte_data(fd, reg) for reg in REGISTERS}
        finally:
            os.close(fd)
    except OSError as error:
        print(f"bus={number} address=0x26 error={error}", flush=True)
    else:
        print(f"bus={number} address=0x26 registers={readings}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("buses", nargs="+", type=int)
    args = parser.parse_args()
    for bus in args.buses:
        if bus not in (0, 1, 2):
            parser.error("only the three known non-display buses 0, 1, 2 are allowed")
        probe_bus(bus)


if __name__ == "__main__":
    main()
