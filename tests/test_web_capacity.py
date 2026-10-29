"""Capacity display uses measured bytes and rejects malformed guest output."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import r1_web_capacity as capacity  # noqa: E402


class WebCapacityTests(unittest.TestCase):
    def test_nvme_reads_recent_bytes(self):
        def run(args, _timeout):
            if args[2].endswith("/status"):
                return ""
            total, used = (1000, 60) if "/local/" in args[2] else (9000, 440)
            return json.dumps([{"time": 1000, "total": total, "used": used}])

        with patch.object(capacity, "_run", side_effect=run), patch.object(capacity.time, "time", return_value=1100):
            self.assertEqual(capacity._nvme_capacity(), (500, 9500, 5))
        with patch.object(capacity, "_run", side_effect=run), patch.object(capacity.time, "time", return_value=1400):
            self.assertIsNone(capacity._nvme_capacity())

    def test_nvme_prefers_actual_available_bytes(self):
        def run(args, _timeout):
            row = {"total": 1000, "used": 60, "avail": 900} if "/local/" in args[2] else {"total": 9000, "used": 440, "avail": 8500}
            return json.dumps(row)

        with patch.object(capacity, "_run", side_effect=run):
            self.assertEqual(capacity._nvme_capacity(), (500, 9400, 5))

    def test_zero_lvm_status_falls_back_to_recent_rrd(self):
        def run(args, _timeout):
            local = "/local/" in args[2]
            if args[2].endswith("/status"):
                return json.dumps({"total": 1000, "used": 60, "avail": 900} if local else
                                  {"total": 0, "used": 0, "avail": 0})
            return json.dumps([{"time": 1000, "total": 1000 if local else 9000,
                                "used": 60 if local else 440}])

        with patch.object(capacity, "_run", side_effect=run), patch.object(capacity.time, "time", return_value=1100):
            self.assertEqual(capacity._nvme_capacity(), (500, 9460, 5))

    def test_guest_accepts_only_valid_numeric_capacity(self):
        config = ("192.0.2.4", "fs", 100, Path("/key"), Path("/known"))
        output = ("SATA 4 4\nCAPACITY 4 4 1000 40 960\n"
                  "CAPACITY 5 97 1000 970 30\n"
                  "CAPACITY 6 80 1000 1200 0\n"
                  "CAPACITY 7 10 1000 100 1000\n")
        with patch.object(capacity, "_run", return_value=output):
            self.assertEqual(capacity._guest_capacity(config),
                             {4: (40 * 1024, 960 * 1024, 4), 5: (970 * 1024, 30 * 1024, 97)})


if __name__ == "__main__":
    unittest.main()
