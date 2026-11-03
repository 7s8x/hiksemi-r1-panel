"""Prepare a full frame and use fbdev pwrite to trigger i915 damage tracking."""
import os


def write_frame(fd, pixels, stride, line_length, width, height):
    if (width <= 0 or height <= 0 or stride < width * 4 or line_length < width * 4 or
            len(pixels) < stride * height):
        raise ValueError("invalid framebuffer layout")
    if stride == line_length == width * 4:
        frame = memoryview(pixels)[:line_length * height]
    else:
        padded = bytearray(line_length * height)
        for y in range(height):
            padded[y * line_length:y * line_length + width * 4] = pixels[y * stride:y * stride + width * 4]
        frame = memoryview(padded)
    offset = 0
    while offset < len(frame):
        count = os.pwrite(fd, frame[offset:], offset)
        if count <= 0 or count > len(frame) - offset:
            raise OSError("short framebuffer write")
        offset += count
