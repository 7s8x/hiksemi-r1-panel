"""Offline checks for honest disk-usage reporting."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import r1_storage_usage as usage  # noqa: E402


class StorageUsageTests(unittest.TestCase):


    def test_guest_ssh_retains_port_and_pinned_host(self):
        config = ("192.0.2.4", "fs", 100, Path("/key"), Path("/known"), 233)
        command = usage._guest_ssh_command(config)
        self.assertEqual(command[command.index("-p") + 1], "233")
        self.assertIn("HostKeyAlias=192.0.2.4", command)
        self.assertIn("StrictHostKeyChecking=yes", command)

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

    def test_missing_guest_config_never_queries_or_guesses_capacity(self):
        from r1_collection import collect_slow
        with (patch("r1_web_capacity._config", return_value=None),
              patch("r1_web_capacity._guest_capacity", side_effect=AssertionError("unexpected SSH"))):
            self.assertEqual(collect_slow("dsm", (("BAY 1", "disk"),)), {"capacity": [None]})


if __name__ == "__main__":
    unittest.main()
