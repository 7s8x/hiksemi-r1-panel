"""Minimal R1 light toggle. Only the verified mode register is written."""

from __future__ import annotations

import os

from r1_rgb_probe import I2C_SLAVE, RGB_ADDRESS, ioctl_checked, read_byte_data
from r1_rgb_trial import BUS_PATH, REG_MODE, write_byte_data


class RgbControl:
    def __init__(self):
        self.last_on_mode = 2  # Known working rainbow mode after a panel restart.

    def _open(self):
        fd = os.open(BUS_PATH, os.O_RDWR | os.O_CLOEXEC)
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
