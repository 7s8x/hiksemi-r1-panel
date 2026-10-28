"""Offline checks for honest disk-usage reporting."""

import sys
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import r1_storage_usage as usage  # noqa: E402


class StorageUsageTests(unittest.TestCase):
    def test_nvme_combines_host_stores(self):
        status = ("Name Type Status Total Used Available %\n"
                  "local dir active 100 60 40 60%\n"
                  "local-lvm lvmthin active 900 40 860 4.44%\n")
        with patch.object(usage, "_run", return_value=status):
            self.assertEqual(usage._nvme_usage(), 10)
        with patch.object(usage, "_run", return_value="local dir active 100 60 40 60%"):
            self.assertIsNone(usage._nvme_usage())

    def test_nvme_uses_recent_pve_history_when_web_cannot_query_lvm(self):
        def command(args, _timeout):
            if args[0] == "pvesm":
                return "local dir active 100 60 40 60%\n"
            used = 60 if "/local/" in args[2] else 40
            total = 100 if "/local/" in args[2] else 900
            return json.dumps([{"time": 1000, "total": total, "used": used}])

        with patch.object(usage, "_run", side_effect=command), patch.object(usage.time, "time", return_value=1100):
            self.assertEqual(usage._nvme_usage(), 10)
        with patch.object(usage, "_run", side_effect=command), patch.object(usage.time, "time", return_value=1400):
            self.assertIsNone(usage._nvme_usage())

    def test_guest_rejects_invalid_or_unrelated_output(self):
        config = ("192.0.2.4", "fs", 100, Path("/key"), Path("/known"))
        with patch.object(usage, "_run", return_value="SATA 3 87\nSATA 4 4\nSATA 5 120\nother 2 60\n"):
            self.assertEqual(usage._guest_usage(config), {3: 87, 4: 4})

    def test_bay_map_follows_current_vm_slot_not_disk_name_or_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("sda", "sdb", "sdc"):
                (root / name).touch()
            (root / "disk-a").symlink_to(root / "sdb")
            (root / "disk-b").symlink_to(root / "sda")
            disks = (("BAY 1", "disk-a"), ("BAY 2", "disk-b"),
                     ("BAY 3", None), ("BAY 4", "missing"))
            host_slots = {(root / "sdb").resolve(): 3, (root / "sda").resolve(): 5}
            guest = {4: 4, 6: 97}
            self.assertEqual(usage._map_guest_to_bays(disks, host_slots, guest, root),
                             [4, 97, None, None])

    def test_missing_guest_config_never_guesses_sata_percent(self):
        with (patch.object(usage, "_nvme_usage", return_value=6),
              patch.object(usage, "_config", return_value=None),
              patch.object(usage, "_cached_usage", None)):
            self.assertEqual(usage.storage_usage((("BAY 1", "disk"),)), [6, None])


if __name__ == "__main__":
    unittest.main()
