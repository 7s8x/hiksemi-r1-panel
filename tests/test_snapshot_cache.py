"""Cache behavior for snapshot-time external commands and hwmon discovery."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import r1_panel_repository as repo  # noqa: E402


class SnapshotCacheTests(unittest.TestCase):
    def test_network_address_caches_within_ttl(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            class R:
                stdout = "192.168.50.3/24 scope global vmbr0\n"
            return R()

        with patch.object(repo.subprocess, "run", fake_run):
            first = repo.network_address()
            second = repo.network_address()
        self.assertEqual(first, "192.168.50.3")
        self.assertEqual(second, "192.168.50.3")
        self.assertEqual(len(calls), 1, "second call should hit cache, not re-run `ip`")

    def test_vm_status_caches_per_vmid_within_ttl(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(tuple(args))
            class R:
                stdout = "status: running\n"
            return R()

        with patch.object(repo.subprocess, "run", fake_run):
            a1 = repo.vm_status(100)
            a2 = repo.vm_status(100)
            b1 = repo.vm_status(111)
        self.assertEqual(a1, "RUNNING")
        self.assertEqual(a2, "RUNNING")
        self.assertEqual(b1, "RUNNING")
        # 100 is cached (1 spawn); 111 is a different VM (1 spawn) => 2 total.
        self.assertEqual(len(calls), 2)
        self.assertIn(("qm", "status", "100"), calls)
        self.assertIn(("qm", "status", "111"), calls)

    def test_drive_temperature_matches_by_block_device_not_dev_node(self):
        # Regression: the cached topology stores each hwmon entry's ``device``
        # as a resolved sysfs path.  drive_temperature must compare it against
        # the disk's /sys/block/<disk>/device path, *not* the /dev/sdX node,
        # or SATA drive temperatures are silently dropped (PR #12 bug).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hwmon = root / "hwmon" / "hwmon0"
            hwmon.mkdir(parents=True)
            (hwmon / "name").write_text("drivetemp\n")
            (hwmon / "temp1_input").write_text("39000\n")
            block_device = root / "sys" / "block" / "sda" / "device"
            block_device.mkdir(parents=True)
            (hwmon / "device").symlink_to(block_device, target_is_directory=True)

            by_id_root = root / "dev" / "disk" / "by-id"
            by_id_root.mkdir(parents=True)
            dev_sda = root / "dev" / "sda"
            dev_sda.mkdir(parents=True)
            (by_id_root / "ata-ST1000_sda").symlink_to(dev_sda, target_is_directory=True)

            result = repo.drive_temperature(
                "ata-ST1000_sda",
                hwmon_root=root / "hwmon",
                by_id_root=by_id_root,
                block_root=root / "sys" / "block",
            )
            self.assertEqual(result, 39.0)

    def test_hwmon_topology_skips_gone_device_during_hotplug(self):
        # Regression: a sensor that vanishes mid-scan (e.g. hot-unplug) must be
        # skipped, not abort the whole topology walk and the snapshot round.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "hwmon" / "hwmon0"
            good.mkdir(parents=True)
            (good / "name").write_text("coretemp\n")
            (good / "temp1_input").write_text("41000\n")
            # A "directory" that is actually a regular file simulates a sysfs
            # node that disappeared between glob and iterdir/read_text.
            gone = root / "hwmon" / "hwmon1"
            gone.write_text("")

            entries = repo._hwmon_topology(root / "hwmon")
            names = [entry["name"] for entry in entries]
            self.assertEqual(names, ["coretemp"], "gone hwmon entry must be skipped, not fatal")
            self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
