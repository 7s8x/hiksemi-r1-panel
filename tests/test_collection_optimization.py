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
    def test_vm_inventory_and_selection_are_independent(self):
        import r1_collection as shared
        from tempfile import TemporaryDirectory
        with patch.object(shared, 'vm_inventory', return_value={100:"RUNNING", 111:"RUNNING", 200:"STOPPED"}) as query:
            payload = shared.collect_slow('vm', ())
        query.assert_called_once_with({})
        record = {'schema': 1, 'boot': 'test', 'published_at': 100, 'groups': {
            'vm': {'valid': True, 'at': 100, 'interval': 60, 'data': json.loads(json.dumps(payload))}}}
        with TemporaryDirectory() as tmp:
            selection = Path(tmp) / 'vm-monitor.json'
            with patch.object(vm, 'SELECTION_FILE', selection):
                selection.write_text('{"vmids":[100,200]}')
                data = shared.decode_snapshot(record, now=100, boot='test')
                self.assertEqual(data['vm_inventory'], [100, 111, 200])
                self.assertEqual(data['vms'], [('VM 100','RUNNING'), ('VM 200','STOPPED')])
                record['groups']['vm']['data']['names'] = {'100': 'Synology'}
                selection.write_text('{"vmids":[100,200],"labels":{"100":{"mode":"pve"},"200":{"mode":"custom","name":"家庭助手"}}}')
                data = shared.decode_snapshot(record, now=100, boot='test')
                self.assertEqual(data['vm_display_names'], {'VM 100':'Synology'})
                record['groups']['vm']['valid'] = False
                data = shared.decode_snapshot(record, now=100, boot='test')
                self.assertNotIn('vm_inventory', data)
                self.assertNotIn('vm_display_names', data)
                self.assertEqual(data['vms'], [('VM 100','UNKNOWN'), ('VM 200','UNKNOWN')])
                selection.write_text('{"vmids":[]}')
                self.assertEqual(shared.decode_snapshot(record, now=100, boot='test')['vms'], [])

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
