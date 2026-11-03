"""Distribution configuration and empty/missing bay protection."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from r1_site_config import SiteConfig, load_site, bay_states
from r1_fan_curve import Reading, target_pwms, temperatures
import r1_vm_status as vms
import r1_doctor as doctor


class SiteConfigTests(unittest.TestCase):
    def test_zero_to_four_expected_bays(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "site.json"
            for count in range(5):
                file.write_text(json.dumps({"occupied_bays": list(range(1, count + 1))}))
                config = load_site(file)
                self.assertEqual(len(config.occupied_bays), count)
                states = bay_states(tuple((f"BAY {i}", f"disk{i}" if i <= count else None) for i in range(1, 5)), site=config)
                self.assertEqual(states, ("present",) * count + ("empty",) * (4 - count))

    def test_bad_config_is_not_silently_replaced_with_defaults(self):
        cases = [{"vmids": [True]}, {"vmids": [100, 100]}, {"vmids": [100, 101, 102]},
                 {"node": "../../other"}, {"stores": ["local", "local"]},
                 {"occupied_bays": [0]}, {"occupied_bays": [1, 1]},
                 {"controller": "bad"}, {"unknown": "ignored?"},
                 {"bay_ports": [[1, 1], [2, 1], [3, 3], [4, 4]]}, []]
        with TemporaryDirectory() as directory:
            file = Path(directory) / "site.json"
            with self.assertRaises(ValueError):
                load_site(file)
            for record in cases:
                with self.subTest(record=record):
                    file.write_text(json.dumps(record))
                    with self.assertRaises(ValueError):
                        load_site(file)

    def test_missing_unexpected_and_zero_disk_policy(self):
        config = replace(SiteConfig(), occupied_bays=(1,))
        self.assertEqual(bay_states((("BAY 1", None), ("BAY 2", "disk"), ("BAY 3", None), ("BAY 4", None)), site=config),
                         ("missing", "unexpected", "empty", "empty"))
        reading = Reading(85, None, 0, 1000, 3000, 120, 120, 1, 1)
        self.assertEqual(target_pwms(reading, 0), (160, 200))
        self.assertEqual(target_pwms(replace(reading, drive_count=-1), 0), (255, 255))
        self.assertEqual(target_pwms(reading, 1), (255, 255))

    def test_sensor_identity_duplicate_and_disappearance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cpu, drive = root / "hwmon0", root / "hwmon8"
            for path, name in ((cpu, "coretemp"), (drive, "drivetemp")):
                path.mkdir(); (path / "name").write_text(name); (path / "temp1_input").write_text("40000")
            block = root / "block"; (block / "sda").mkdir(parents=True)
            device = root / "physical"; device.mkdir()
            (block / "sda/device").symlink_to(device)
            (drive / "device").symlink_to(device)
            by_id = root / "by-id"; by_id.mkdir()
            disk = root / "sda"; disk.touch(); (by_id / "ata-example").symlink_to(disk)
            site = replace(SiteConfig(), occupied_bays=(1,))
            disks = (("BAY 1", "ata-example"), ("BAY 2", None), ("BAY 3", None), ("BAY 4", None))
            with patch("r1_fan_curve.discover_bays", return_value=disks):
                self.assertEqual(temperatures(root, block_root=block, by_id_root=by_id, site=site), (40, 40, 1))
                # Rename a hwmon directory: no cached path may survive.
                drive.rename(root / "hwmon9")
                self.assertEqual(temperatures(root, block_root=block, by_id_root=by_id, site=site), (40, 40, 1))
                duplicate = root / "hwmon10"; duplicate.mkdir()
                (duplicate / "name").write_text("drivetemp"); (duplicate / "device").symlink_to(device)
                (duplicate / "temp1_input").write_text("40000")
                self.assertEqual(temperatures(root, block_root=block, by_id_root=by_id, site=site), (40, None, -1))

    def test_custom_vm_node_and_no_vm_query_when_empty(self):
        from types import SimpleNamespace
        with patch.object(vms, "SITE", replace(SiteConfig(), node="example", vmids=(120,))), patch.object(vms.subprocess, "run", return_value=SimpleNamespace(stdout='[{"vmid":120,"status":"running"}]')) as run:
            self.assertEqual(vms.vm_statuses(), {120: "RUNNING"})
            self.assertIn("/nodes/example/qemu", run.call_args.args[0])
        with patch.object(vms, "SITE", replace(SiteConfig(), vmids=())), patch.object(vms.subprocess, "run") as run:
            self.assertEqual(vms.vm_statuses(), {})
            run.assert_not_called()

    def test_doctor_never_loads_driver_and_rejects_wrong_hash(self):
        with TemporaryDirectory() as directory, patch.object(doctor.subprocess, "run") as run:
            module = Path(directory) / "it87.ko"; module.write_bytes(b"fixture")
            self.assertFalse(doctor.check_module(str(module), "0" * 64))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
