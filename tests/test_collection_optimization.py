"""Check shared capacity source and bounded host identity caching."""
import sys
import json
import subprocess
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import r1_panel_repository as panel
import r1_vm_status as vm


class CollectionOptimizationTests(unittest.TestCase):
    def test_batch_vm_ids_order_and_paused_guest_meaning(self):
        records = [{"vmid": 111, "status": "running", "qmpstatus": "paused"},
                   {"vmid": 101, "status": "stopped"},
                   {"vmid": 100, "status": "stopped"}]
        with patch.object(vm.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(records))) as run:
            self.assertEqual(vm.vm_statuses(), {100: "STOPPED", 111: "RUNNING"})
        run.assert_called_once()

    def test_batch_vm_invalid_missing_duplicate_and_recovery(self):
        samples = ["bad json", "{}", '[{"vmid":100,"status":"running"},{"vmid":100,"status":"stopped"}]',
                   '[null,{"vmid":true,"status":"running"},{"vmid":111,"status":"bad"}]',
                   '[{"vmid":100,"status":"running"}]']
        with patch.object(vm.subprocess, "run", side_effect=[SimpleNamespace(stdout=s) for s in samples]):
            for _ in samples[:-1]:
                self.assertEqual(vm.vm_statuses(), {100: "UNKNOWN", 111: "UNKNOWN"})
            self.assertEqual(vm.vm_statuses(), {100: "RUNNING", 111: "UNKNOWN"})

    def test_batch_vm_timeout_does_not_reuse_running(self):
        with patch.object(vm.subprocess, "run", side_effect=[
                SimpleNamespace(stdout='[{"vmid":100,"status":"running"}]'),
                subprocess.TimeoutExpired("pvesh", 4)]):
            self.assertEqual(vm.vm_statuses()[100], "RUNNING")
            self.assertEqual(vm.vm_statuses()[100], "UNKNOWN")

    def test_screen_reads_shared_snapshot_without_host_queries(self):
        expected = {"storage_capacity": [(60, 940, 6), None], "vms": []}
        with patch.object(panel, "read_snapshot", return_value=expected) as read, \
             patch.object(panel, "discover_drives", side_effect=AssertionError("duplicate collection")), \
             patch("subprocess.run", side_effect=AssertionError("duplicate collection")):
            self.assertEqual(panel.screen_snapshot(), expected)
        read.assert_called_once_with()

    def test_identity_scheduling_does_not_add_a_second_cache(self):
        import r1_collection as shared
        with patch('r1_host_sampling.system_identity', side_effect=[
                ('PROXMOX VE 9.1.1', 'CPU'), ('PROXMOX VE --', 'CPU')]) as read:
            self.assertEqual(shared.collect_slow('identity', ())['pve_version'], 'PROXMOX VE 9.1.1')
            self.assertEqual(shared.collect_slow('identity', ())['pve_version'], 'PROXMOX VE --')
        self.assertEqual(read.call_count, 2)

    def test_decimal_device_size_labels_preserve_existing_rounding(self):
        from r1_host_sampling import _format_size
        for value, expected in ((0, '--'), (500, '500GB'), (999, '999GB'),
                                (1000, '1TB'), (1920, '2TB'), (4000, '4TB')):
            with self.subTest(value=value):
                self.assertEqual(_format_size(value), expected)


if __name__ == "__main__":
    unittest.main()
