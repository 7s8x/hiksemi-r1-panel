"""Narrow, reversible RGB protocol trial for the verified R1 I2C-0/0x26.

Only four known candidate registers are touched. This is a temporary manual
test, not a background lighting daemon or proof of visual LED behavior.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import time

from r1_rgb_probe import (
    I2C_SLAVE,
    I2C_SMBUS,
    I2C_SMBUS_BYTE_DATA,
    RGB_ADDRESS,
    SmbusData,
    SmbusIoctlData,
    ioctl_checked,
    read_byte_data,
)

I2C_SMBUS_WRITE = 0
BUS_PATH = "/dev/i2c-0"
REG_MODE = 0x90
REG_RED = 0x92
REG_GREEN = 0x93
REG_BLUE = 0x94
REGISTERS = (REG_MODE, REG_RED, REG_GREEN, REG_BLUE)
BASELINE = (2, 255, 0, 0)


def write_byte_data(fd: int, register: int, value: int) -> None:
    if register not in REGISTERS or not 0 <= value <= 255:
        raise ValueError("register or value is outside the narrow RGB trial scope")
    data = SmbusData()
    data.byte = value
    operation = SmbusIoctlData(
        I2C_SMBUS_WRITE, register, I2C_SMBUS_BYTE_DATA, ctypes.pointer(data)
    )
    ioctl_checked(fd, I2C_SMBUS, ctypes.byref(operation))


def read_state(fd: int) -> tuple[int, int, int, int]:
    return tuple(read_byte_data(fd, register) for register in REGISTERS)


def set_state(fd: int, mode: int, red: int, green: int, blue: int) -> None:
    if mode not in (0, 1, 2, 3):
        raise ValueError("unsupported mode")
    for register, value in ((REG_RED, red), (REG_GREEN, green), (REG_BLUE, blue)):
        write_byte_data(fd, register, value)
    write_byte_data(fd, REG_MODE, mode)
    actual = read_state(fd)
    expected = (mode, red, green, blue)
    if actual != expected:
        raise RuntimeError(f"RGB readback mismatch: expected={expected} actual={actual}")
    print(f"readback={actual}", flush=True)


def run_trial(fd: int) -> None:
    original = read_state(fd)
    print(f"original={original}", flush=True)
    if original != BASELINE:
        raise RuntimeError("RGB state changed since rollback timer was prepared")
    try:
        write_byte_data(fd, REG_MODE, 0)
        if read_byte_data(fd, REG_MODE) != 0:
            raise RuntimeError("off-mode readback failed")
        print("off-mode readback=0", flush=True)
        time.sleep(5)
        set_state(fd, 1, 0, 0, 64)
        time.sleep(5)
        set_state(fd, 1, 64, 0, 0)
        time.sleep(5)
    finally:
        set_state(fd, *original)
        print("baseline restored", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("read", "trial", "restore"))
    args = parser.parse_args()
    fd = os.open(BUS_PATH, os.O_RDWR | os.O_CLOEXEC)
    try:
        ioctl_checked(fd, I2C_SLAVE, RGB_ADDRESS)
        if args.action == "read":
            print(f"state={read_state(fd)}", flush=True)
        elif args.action == "restore":
            set_state(fd, *BASELINE)
            print("safety baseline restored", flush=True)
        else:
            run_trial(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
