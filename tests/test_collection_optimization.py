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
import r1_web_views as views


class CollectionOptimizationTests(unittest.TestCase):
    def test_batch_vm_ids_order_and_paused_guest_meaning(self):
        records = [{"vmid": 111, "status": "running", "qmpstatus": "paused"},
                   {"vmid": 101, "status": "stopped"},
                   {"vmid": 100, "status": "stopped"}]
        with patch.object(panel.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(records))) as run:
            self.assertEqual(panel.vm_statuses(), {100: "STOPPED", 111: "RUNNING"})
        run.assert_called_once()

    def test_batch_vm_invalid_missing_duplicate_and_recovery(self):
        samples = ["bad json", "{}", '[{"vmid":100,"status":"running"},{"vmid":100,"status":"stopped"}]',
                   '[null,{"vmid":true,"status":"running"},{"vmid":111,"status":"bad"}]',
                   '[{"vmid":100,"status":"running"}]']
        with patch.object(panel.subprocess, "run", side_effect=[SimpleNamespace(stdout=s) for s in samples]):
            for _ in samples[:-1]:
                self.assertEqual(panel.vm_statuses(), {100: "UNKNOWN", 111: "UNKNOWN"})
            self.assertEqual(panel.vm_statuses(), {100: "RUNNING", 111: "UNKNOWN"})

    def test_batch_vm_timeout_does_not_reuse_running(self):
        with patch.object(panel.subprocess, "run", side_effect=[
                SimpleNamespace(stdout='[{"vmid":100,"status":"running"}]'),
                subprocess.TimeoutExpired("pvesh", 4)]):
            self.assertEqual(panel.vm_statuses()[100], "RUNNING")
            self.assertEqual(panel.vm_statuses()[100], "UNKNOWN")

    def test_screen_reads_shared_snapshot_without_host_queries(self):
        expected = {"storage_capacity": [(60, 940, 6), None], "vms": []}
        with patch.object(panel, "read_snapshot", return_value=expected) as read, \
             patch.object(panel, "discover_drives", side_effect=AssertionError("duplicate collection")), \
             patch.object(panel, "snapshot", side_effect=AssertionError("duplicate collection")):
            self.assertEqual(panel.screen_snapshot(), expected)
        read.assert_called_once_with()

    def test_identity_expires_and_failure_does_not_serve_old_version(self):
        with patch.object(views, "_identity_cache", None), \
             patch.object(views, "_identity_expires", 0), \
             patch.object(views.time, "monotonic", return_value=100) as clock, \
             patch.object(views, "_read_system_identity", side_effect=[
                 ("PROXMOX VE 9.1.1", "CPU"), ("PROXMOX VE --", "CPU"),
                 ("PROXMOX VE 9.2.0", "CPU")]) as read:
            self.assertEqual(views.system_identity()[0], "PROXMOX VE 9.1.1")
            clock.return_value = 399
            views.system_identity()
            self.assertEqual(read.call_count, 1)
            clock.return_value = 401
            self.assertEqual(views.system_identity()[0], "PROXMOX VE --")
            self.assertEqual(views.system_identity()[0], "PROXMOX VE 9.2.0")


if __name__ == "__main__":
    unittest.main()
