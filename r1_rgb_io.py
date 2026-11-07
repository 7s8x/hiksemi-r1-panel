"""Narrow shared RGB SMBus primitives; importing performs no device access."""
from __future__ import annotations
import ctypes
import os

I2C_SLAVE = 0x0703
I2C_SMBUS = 0x0720
I2C_SMBUS_READ = 1
I2C_SMBUS_BYTE_DATA = 2
RGB_ADDRESS = 0x26


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



I2C_SMBUS_WRITE = 0
REG_MODE = 0x90
REG_RED = 0x92
REG_GREEN = 0x93
REG_BLUE = 0x94
REGISTERS = (REG_MODE, REG_RED, REG_GREEN, REG_BLUE)


def write_byte_data(fd: int, register: int, value: int) -> None:
    if register not in REGISTERS or not 0 <= value <= 255:
        raise ValueError("register or value is outside the narrow RGB trial scope")
    data = SmbusData()
    data.byte = value
    operation = SmbusIoctlData(
        I2C_SMBUS_WRITE, register, I2C_SMBUS_BYTE_DATA, ctypes.pointer(data)
    )
    ioctl_checked(fd, I2C_SMBUS, ctypes.byref(operation))


