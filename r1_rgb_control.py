"""Minimal R1 light toggle. Only the verified mode register is written."""

from __future__ import annotations

import os
from pathlib import Path

from r1_rgb_io import I2C_SLAVE, RGB_ADDRESS, ioctl_checked, read_byte_data
from r1_rgb_io import (REG_MODE, REG_RED, REG_GREEN, REG_BLUE,
                          write_byte_data)


RGB_ADAPTER = Path('/sys/bus/pci/devices/0000:00:15.0/i2c_designware.0')


def rgb_bus_path(adapter=RGB_ADAPTER):
    """Resolve the verified physical adapter, not its boot-dependent i2c-N."""
    buses = sorted(adapter.glob('i2c-[0-9]*'))
    if len(buses) != 1 or (buses[0] / 'name').read_text().strip() != 'Synopsys DesignWare I2C adapter':
        raise RuntimeError('verified RGB I2C adapter unavailable')
    return f'/dev/{buses[0].name}'


class RgbControl:
    def __init__(self):
        self.last_on_mode = 2  # Known working rainbow mode after a panel restart.

    def _open(self):
        fd = os.open(rgb_bus_path(), os.O_RDWR | os.O_CLOEXEC)
        try:
            ioctl_checked(fd, I2C_SLAVE, RGB_ADDRESS)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def status(self):
        fd = self._open()
        try:
            mode = read_byte_data(fd, REG_MODE)
            if mode not in (0, 1, 2, 3):
                raise RuntimeError(f"unsupported RGB mode: {mode}")
            if mode:
                self.last_on_mode = mode
            return mode != 0
        finally:
            os.close(fd)

    def toggle(self):
        fd = self._open()
        try:
            previous = read_byte_data(fd, REG_MODE)
            if previous not in (0, 1, 2, 3):
                raise RuntimeError(f"unsupported RGB mode: {previous}")
            if previous:
                self.last_on_mode = previous
                target = 0
            else:
                target = self.last_on_mode
            write_byte_data(fd, REG_MODE, target)
            actual = read_byte_data(fd, REG_MODE)
            if actual != target:
                # A failed readback should not leave the light in an unknown mode.
                write_byte_data(fd, REG_MODE, previous)
                raise RuntimeError(f"RGB mode readback mismatch: {actual}")
            return target != 0
        finally:
            os.close(fd)

    def read_state(self):
        fd = self._open()
        try:
            return tuple(read_byte_data(fd, register)
                         for register in (REG_MODE, REG_RED, REG_GREEN, REG_BLUE))
        finally:
            os.close(fd)

    def set_state(self, mode: int, red: int, green: int, blue: int):
        if type(mode) is not int or mode not in (0, 1, 2, 3):
            raise ValueError("unsupported RGB mode")
        if any(type(value) is not int or not 0 <= value <= 255
               for value in (red, green, blue)):
            raise ValueError("invalid RGB color")
        fd = self._open()
        try:
            previous = tuple(read_byte_data(fd, register)
                             for register in (REG_MODE, REG_RED, REG_GREEN, REG_BLUE))
            desired = (mode, red, green, blue)
            try:
                for register, value in zip((REG_RED, REG_GREEN, REG_BLUE), desired[1:]):
                    if read_byte_data(fd, register) != value:
                        write_byte_data(fd, register, value)
                if previous[0] != mode:
                    write_byte_data(fd, REG_MODE, mode)
                actual = tuple(read_byte_data(fd, register)
                               for register in (REG_MODE, REG_RED, REG_GREEN, REG_BLUE))
                if actual != desired:
                    raise RuntimeError("RGB readback mismatch")
            except (OSError, RuntimeError):
                for register, value in zip((REG_RED, REG_GREEN, REG_BLUE), previous[1:]):
                    write_byte_data(fd, register, value)
                write_byte_data(fd, REG_MODE, previous[0])
                raise
            if mode:
                self.last_on_mode = mode
            return actual
        finally:
            os.close(fd)
