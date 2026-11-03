"""Pixel equivalence, stride/partial-write correctness and stale data behavior."""
from pathlib import Path
import random
import struct
import sys
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import r1_web as web
from r1_framebuffer import write_frame
from r1_panel_runtime import SnapshotCollector


class RenderPerformanceTests(unittest.TestCase):
    def test_png_decoded_pixels_match_original_loop_with_padded_stride(self):
        for stride in (web.WIDTH * 4, web.WIDTH * 4 + 16):
            raw = random.Random(42).randbytes(stride * web.HEIGHT)
            encoded = web.rgb_png(raw, stride)
            cursor, payload = 8, b""
            while cursor < len(encoded):
                size = struct.unpack(">I", encoded[cursor:cursor + 4])[0]
                kind = encoded[cursor + 4:cursor + 8]
                chunk = encoded[cursor + 8:cursor + 8 + size]
                crc = struct.unpack(">I", encoded[cursor + 8 + size:cursor + 12 + size])[0]
                self.assertEqual(crc, zlib.crc32(kind + chunk))
                if kind == b"IDAT": payload += chunk
                cursor += size + 12
            expected = bytearray()
            for y in range(web.HEIGHT):
                expected.append(0)
                for x in range(web.WIDTH):
                    i = y * stride + x * 4
                    expected.extend((raw[i + 2], raw[i + 1], raw[i]))
            self.assertEqual(zlib.decompress(payload), expected)

    def test_pwrite_fast_path_and_padded_path_preserve_all_pixels(self):
        for stride, line in ((8, 8), (12, 16), (12, 8), (8, 16)):
            raw = bytes(range(stride * 3))
            output = bytearray(line * 3)
            def partial(fd, part, offset):
                size = min(5, len(part))
                output[offset:offset + size] = part[:size]
                return size
            with patch("r1_framebuffer.os.pwrite", side_effect=partial):
                write_frame(7, raw, stride, line, 2, 3)
            for row in range(3):
                self.assertEqual(output[row * line:row * line + 8], raw[row * stride:row * stride + 8])
                self.assertEqual(output[row * line + 8:(row + 1) * line], bytes(max(0, line - 8)))
        with patch("r1_framebuffer.os.pwrite", return_value=0):
            with self.assertRaises(OSError): write_frame(7, bytes(24), 8, 8, 2, 3)
        with self.assertRaises(ValueError): write_frame(7, bytes(23), 8, 8, 2, 3)

    def test_expired_snapshot_never_reuses_old_running_vm_or_temperatures(self):
        collector = SnapshotCollector(lambda: {}, interval=15)
        collector._data = {"cpu": 55, "vms": [(100, "RUNNING")]}
        collector._sample_at = 10
        with patch("r1_panel_runtime.time.monotonic", return_value=39):
            self.assertEqual(collector.latest()[0]["cpu"], 55)
        with patch("r1_panel_runtime.time.monotonic", return_value=41):
            data = collector.latest()[0]
            self.assertIsNone(data["cpu"])
            self.assertEqual(data["vms"][0][1], "UNKNOWN")
            self.assertFalse(data["freshness"]["valid"])
        collector._failed = True
        with patch("r1_panel_runtime.time.monotonic", return_value=11):
            self.assertEqual(collector.latest()[0]["freshness"]["reason"], "collection_failed")


if __name__ == "__main__": unittest.main()
