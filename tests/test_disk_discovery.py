"""Offline checks for automatic SATA disk discovery."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_panel_repository import bay_identity, discover_drives
from r1_collection import collect_fast  # noqa: E402


class DiskDiscoveryTests(unittest.TestCase):
    def test_missing_path_is_unverified_not_assumed_empty(self):
        self.assertEqual(bay_identity("BAY 2", None), ("BAY 2", "UNVERIFIED", "--"))

    def test_discovers_physical_bays_in_order_and_preserves_empty_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            block, by_id, by_path, dev = (root / name for name in ("block", "by-id", "by-path", "dev"))
            for directory in (block, by_id, by_path, dev):
                directory.mkdir()
            for name, port in (("sdb", 4), ("sda", 2), ("sdc", 1)):
                target = root / "0000:04:00.0" / f"ata{port}" / "host0" / "target0:0:0" / "0:0:0:0"
                target.mkdir(parents=True)
                disk = block / name
                disk.mkdir()
                (disk / "device").symlink_to(target, target_is_directory=True)
                (dev / name).touch()
                (by_path / f"pci-0000:04:00.0-ata-{port}.0").symlink_to(dev / name)
            (by_id / "ata-old_disk_SERIAL-A").symlink_to(dev / "sda")
            (by_id / "ata-old_disk_SERIAL-A-part1").symlink_to(dev / "sda")
            usb = root / "usb1" / "host1"
            usb.mkdir(parents=True)
            (block / "sdd").mkdir()
            (block / "sdd" / "device").symlink_to(usb, target_is_directory=True)
            self.assertEqual(discover_drives(block, by_id, by_path),
                             (("BAY 1", "/dev/sdb"),
                              ("BAY 2", None),
                              ("BAY 3", "ata-old_disk_SERIAL-A"),
                              ("BAY 4", "/dev/sdc")))

    def test_fast_collection_refreshes_disk_list_between_samples(self):
        with (patch("r1_host_sampling.discover_drives", side_effect=[(("BAY 1", "old"),), (("BAY 1", "new"),)]),
              patch("r1_host_sampling.disk_identity", side_effect=lambda disk: ("1TB", "HDD", disk)),
              patch("r1_host_sampling.drive_temperature", side_effect=lambda disk, **_: 32 if disk == "old" else 36),
              patch("r1_storage_queries.disk_generation", side_effect=[("old",), ("new",)]),
              patch("r1_host_sampling.memory", return_value=(16, 8)),
              patch("r1_host_sampling.read_text", return_value="3600.0"),
              patch("r1_host_sampling.temp_for_hwmon", return_value=42),
              patch("r1_host_sampling.nvme_identity", return_value=("1TB", "NVME", "NVME")),
              patch("r1_host_sampling.cpu_usage", return_value=20),
              patch("r1_host_sampling.fan_readings", return_value=None),
              patch("r1_host_sampling.drive_models", return_value=[]),
              patch("r1_host_sampling.HwmonSample.temperatures", return_value=(42, 36, 1)),
              patch("subprocess.run", side_effect=AssertionError("fast path queried host"))):
            first, generation1, _ = collect_fast()
            second, generation2, _ = collect_fast()
        self.assertEqual(first["storage"][1], (("BAY 1  1TB", "HDD", "old"), 32))
        self.assertEqual(second["storage"][1], (("BAY 1  1TB", "HDD", "new"), 36))
        self.assertNotEqual(generation1, generation2)


if __name__ == "__main__":
    unittest.main()
