"""Narrow, read-only probe of the R1 Toolbox RGB I2C candidate at 0x26.

This deliberately does not scan addresses, force a busy slave, or write a
register value. Reading SMBus byte-data sends a register selector as part of
the read transaction; that is not a proof of device identity.
"""

from __future__ import annotations

import argparse
import ctypes
import os

I2C_SLAVE = 0x0703
I2C_SMBUS = 0x0720
I2C_SMBUS_READ = 1
I2C_SMBUS_BYTE_DATA = 2
RGB_ADDRESS = 0x26
REGISTERS = (0x90, 0x92, 0x93, 0x94)


class SmbusData(ctypes.Union):
    _fields_ = [
        ("byte", ctypes.c_uint8),
        ("word", ctypes.c_uint16),
        ("block", ctypes.c_uint8 * 34),
    ]


class SmbusIoctlData(ctypes.Structure):
    _fields_ = [
        ("read_write", ctypes.c_uint8),
        ("command", ctypes.c_uint8),
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(SmbusData)),
    ]


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ioctl.restype = ctypes.c_int


def ioctl_checked(fd: int, request: int, argument: object) -> None:
    if LIBC.ioctl(fd, request, argument) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def read_byte_data(fd: int, register: int) -> int:
    value = SmbusData()
    operation = SmbusIoctlData(
        I2C_SMBUS_READ, register, I2C_SMBUS_BYTE_DATA, ctypes.pointer(value)
    )
    ioctl_checked(fd, I2C_SMBUS, ctypes.byref(operation))
    return value.byte


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
