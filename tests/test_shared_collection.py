"""Exercise scheduling, failure boundaries and consumer isolation without hardware."""
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import r1_collection as shared


class CollectionTests(unittest.TestCase):
    def make_collector(self, slow=None):
        self.now = 100.0
        self.generation = ['disk-a']
        return shared.Collector(clock=lambda: self.now, boot='boot',
            fast=lambda: ({'cpu': 55}, list(self.generation), (('BAY 1', 'disk'),)),
            slow=slow or (lambda key, disks: {'capacity': [[6, 94, 6]] * 4}))

    def test_independent_schedule_over_thirty_minutes(self):
        calls = []
        c = self.make_collector(lambda key, disks: calls.append((key, self.now)) or {})
        with patch('builtins.print'):
            for second in range(1800):
                self.now = 100 + second
                if second % 5 == 0:
                    c.sample_fast()
                while c.slow_once():
                    pass
        self.assertEqual(sum(key == 'vm' for key, _ in calls), 30)
        self.assertEqual(sum(key == 'nvme' for key, _ in calls), 6)
        self.assertEqual(sum(key == 'dsm' for key, _ in calls), 1)
        self.assertEqual(shared.INTERVALS['dsm'], 1800)

    def test_blocked_slow_job_does_not_block_fast_or_publication(self):
        entered, release = threading.Event(), threading.Event()
        def slow(key, disks):
            entered.set()
            release.wait(2)
            return {}
        c = self.make_collector(slow)
        c.sample_fast()
        worker = threading.Thread(target=c.slow_once)
        with patch('builtins.print'), tempfile.TemporaryDirectory() as directory:
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.now += 5
                c.sample_fast()
                target = Path(directory) / 'runtime' / 'snapshot.json'
                c.publish(target)
                result = shared.read_snapshot(target, now=self.now, boot='boot')
                self.assertEqual(result['cpu'], 55)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_generation_change_invalidates_capacity_and_discards_inflight_result(self):
        c = self.make_collector()
        c.sample_fast()
        self.now += 20
        c.sample_fast()
        with patch('builtins.print'):
            while c.slow_once():
                pass
        self.assertEqual(shared.decode_snapshot(c.export(), now=self.now, boot='boot')['storage_usage'][1], 6)
        self.generation = ['disk-b']
        c.sample_fast()
        self.assertIsNone(shared.decode_snapshot(c.export(), now=self.now, boot='boot')['storage_usage'][1])
        def changed_during_query(key, disks):
            self.generation = ['disk-c']
            c.sample_fast()
            return {'capacity': [[90, 10, 90]] * 4}
        c.slow = changed_during_query
        self.now += 10
        with patch('builtins.print'):
            c.slow_once()
        self.assertFalse(c.groups['nvme']['valid'])
        self.assertEqual(c.due['nvme'], self.now + 10)

    def test_failure_and_expiry_never_renew_old_values(self):
        c = self.make_collector(lambda key, disks: {'vms': [[100, 'RUNNING']]})
        c.sample_fast()
        with patch('builtins.print'):
            c.slow_once()
            self.now += 60
            c.slow = lambda *_: (_ for _ in ()).throw(TimeoutError())
            c.slow_once()
        c.sample_fast()
        record = c.export()
        result = shared.decode_snapshot(record, now=self.now, boot='boot')
        self.assertEqual(result['vms'][0][1], 'UNKNOWN')
        self.assertEqual(result['cpu'], 55)
        result = shared.decode_snapshot(record, now=self.now + 11, boot='boot')
        self.assertIsNone(result['cpu'])
        self.assertFalse(result['freshness']['valid'])

    def test_malformed_boot_future_and_missing_file_fail_closed(self):
        c = self.make_collector()
        c.sample_fast()
        record = c.export()
        for boot, now in [('other', self.now), ('boot', self.now - 1), ('boot', self.now + 121)]:
            self.assertIsNone(shared.decode_snapshot(record, now=now, boot=boot)['cpu'])
        record['groups'] = {key: [] for key in shared.INTERVALS}
        self.assertFalse(shared.decode_snapshot(record, now=self.now, boot='boot')['freshness']['valid'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing'
            with patch('subprocess.run', side_effect=AssertionError('reader queried host')):
                self.assertIsNone(shared.read_snapshot(path)['cpu'])
                path.write_text('invalid json')
                self.assertIsNone(shared.read_snapshot(path)['cpu'])
                path.write_bytes(b' ' * (shared.MAX_BYTES + 1))
                self.assertIsNone(shared.read_snapshot(path)['cpu'])

    def test_nvme_refresh_cannot_query_dsm(self):
        with patch('r1_storage_queries.nvme_capacity', return_value=(1, 9, 10)) as nvme, \
             patch('r1_storage_queries.guest_capacity', side_effect=AssertionError('unexpected SSH')):
            self.assertEqual(shared.collect_slow('nvme', ()), {'capacity': [(1, 9, 10)]})
        nvme.assert_called_once_with()

    def test_fast_path_never_executes_external_commands(self):
        import r1_host_sampling as panel
        with patch('subprocess.run', side_effect=AssertionError('fast path spawned process')), \
             patch.object(panel, 'discover_drives', return_value=()), \
             patch.object(panel, 'read_text', return_value='100 0'), \
             patch.object(panel, 'memory', return_value=(16, 8)), \
             patch.object(panel, 'cpu_usage', return_value=28), \
             patch.object(panel, 'temp_for_hwmon', return_value=45), \
             patch.object(panel, 'nvme_identity', return_value=('1TB', 'NVME', 'private')), \
             patch.object(panel, 'fan_readings', return_value=None), \
             patch('r1_host_sampling.HwmonSample.temperatures', return_value=(45, None, 0)):
            result, _, _ = shared.collect_fast()
        self.assertEqual(result['cpu_usage'], 28)

    def test_rgb_polling_is_cached_but_control_readback_can_refresh(self):
        import r1_panel_repository as panel
        with patch.object(panel, '_RGB_CACHE', None), patch.object(panel, '_RGB_EXPIRES', 0), \
             patch.object(panel.time, 'monotonic', return_value=100) as clock, \
             patch.object(panel.RGB, 'read_state', side_effect=[(2, 255, 0, 0), (0, 255, 0, 0), OSError()]) as read:
            self.assertTrue(panel.rgb_status())
            for _ in range(10):
                self.assertEqual(panel.rgb_state_cached()[0], 2)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(panel.rgb_state_cached(force=True)[0], 0)
            clock.return_value = 116
            self.assertIsNone(panel.rgb_status())
            self.assertIsNone(panel.rgb_status())
            self.assertEqual(read.call_count, 3)
            panel.remember_rgb_state((2, 1, 2, 3))
            self.assertTrue(panel.rgb_status())
            self.assertEqual(read.call_count, 3)

    def test_invalid_intervals_are_rejected(self):
        for value in (0, -1, float('nan'), True, 86401):
            with self.assertRaises(ValueError):
                shared.Collector({'dsm': value}, boot='boot')

    def test_new_fast_publication_does_not_renew_capacity_age(self):
        c = self.make_collector()
        c.sample_fast()
        c.groups['dsm'] = {'valid': True, 'at': self.now, 'interval': 1800,
                           'generation': self.generation, 'data': {'capacity': [[6, 94, 6]] * 4}}
        self.now += 3601
        c.sample_fast()
        result = shared.decode_snapshot(c.export(), now=self.now, boot='boot')
        self.assertEqual(result['cpu'], 55)
        self.assertFalse(result['freshness']['groups']['dsm'])
        self.assertEqual(result['storage_usage'], [None] * 5)


if __name__ == '__main__':
    unittest.main()
