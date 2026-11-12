"""Review regressions: no hardware or production configuration is accessed."""
import copy
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import r1_fan_config as config
import r1_fan_curve as fan
import r1_web as web


class ProfileReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.high, self.low = root / 'high.json', root / 'low.json'
        self.stack.enter_context(patch.object(config, 'CONFIG_FILE', self.high))
        self.stack.enter_context(patch.object(config, 'LOW_CONFIG_FILE', self.low))
        self.stack.enter_context(patch.object(web, '_web_log'))
        self.after = copy.deepcopy(config.DEFAULT)
        self.after['cpu'][-2][1] += 1

    def run_controller(self, loads, ticks=1, mode='high', digest=None):
        clock = iter([0] + [v for _ in range(ticks) for v in (0, 1)] + [2])
        with (patch.object(fan, 'sample', return_value=fan.Reading(75, 50, 4, 995, 2860, 120, 120, 1, 1)),
              patch.object(fan, 'load', side_effect=loads) as loader,
              patch.object(fan, 'set_channels') as write,
              patch.object(fan, 'restore') as restore,
              patch.object(fan, 'requested_mode', return_value=mode),
              patch.object(fan, 'publish_status') as status,
              patch.object(fan, 'time') as clock_mock,
              patch.object(fan, 'config_path') as path):
            path.return_value.stat.return_value.st_mtime_ns = 1
            clock_mock.monotonic.side_effect = lambda: next(clock)
            if digest is not None:
                self.stack.enter_context(patch.object(fan, 'bundle_digest', side_effect=digest))
            fan.run(1, Path('/unused'))
        restore.assert_called_once()
        return write, status, loader

    def test_corrupt_unused_profile_at_start_keeps_default_controller_running(self):
        write, status, _ = self.run_controller([config.DEFAULT, ValueError('bad low')])
        write.assert_called_once_with(Path('/unused'), 180, 160)
        self.assertEqual(status.call_args.kwargs['curve_id'], config.bundle_digest({p: config.DEFAULT for p in config.PROFILES}))

    def test_corrupt_config_does_not_block_full_speed(self):
        write, _, _ = self.run_controller([PermissionError('unreadable')], mode='full')
        write.assert_called_once_with(Path('/unused'), 255, 255)

    def test_digest_never_rereads_validated_records(self):
        records = config.load_profiles()
        with patch.object(config, 'load', side_effect=AssertionError('second read')):
            self.assertEqual(config.bundle_digest(records), config.bundle_digest(records))

    def test_runtime_rejection_preserves_previous_curves_and_id(self):
        # A changed mtime must not let a partially validated set replace the old set.
        low = copy.deepcopy(config.DEFAULT)
        low['cpu'] = [[55, 120], [70, 120], [80, 140], [90, 180], [95, 255]]
        low['drive'] = [[35, 120], [45, 120], [50, 140], [55, 200], [60, 255]]
        with (patch.object(fan, 'sample', return_value=fan.Reading(75, 50, 4, 995, 2860, 120, 120, 1, 1)),
              patch.object(fan, 'load', side_effect=[config.DEFAULT, low, self.after, ValueError('bad low')]),
              patch.object(fan, 'config_path') as path,
              patch.object(fan, 'set_channels') as write, patch.object(fan, 'restore'),
              patch.object(fan, 'requested_mode', return_value='low'),
              patch.object(fan, 'publish_status') as status, patch.object(fan, 'time') as clock):
            path.return_value.stat.side_effect = [SimpleNamespace(st_mtime_ns=n) for n in (1, 1, 2, 2)]
            clock.monotonic.side_effect = [0, 0, 1, 0, 1, 2]
            fan.run(1, Path('/unused'))
        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args_list[0], write.call_args_list[1])
        self.assertEqual(status.call_args_list[0].kwargs['curve_id'], status.call_args_list[1].kwargs['curve_id'])

    def handler(self, path, body=None):
        return SimpleNamespace(path=path, headers={'X-R1-CSRF': 'test'},
            _allowed=lambda: True, _origin_ok=lambda: True,
            _session=lambda: ('session', 'test'), _body=lambda: body, _json=Mock())

    def test_bad_low_file_returns_status_with_error_not_disconnect(self):
        self.low.write_text('{broken')
        h = self.handler('/api/status')
        with patch.object(web, 'read_state', return_value={}), patch.object(web, 'control_status', return_value={'live': True}):
            web.Handler.do_GET(h)
        code, result = h._json.call_args.args
        self.assertEqual(code, 200)
        self.assertIsNone(result['curve_id'])
        self.assertTrue(result['curve_error'])
        self.assertEqual(result['profiles']['low'], config.DEFAULT)

    def test_bad_sibling_is_rejected_before_save(self):
        self.low.write_text('{broken')
        with patch.object(web, 'save') as save:
            with self.assertRaises(ValueError): web.apply_curve(self.after, 'high')
        save.assert_not_called()

    def test_success_confirms_exact_record_digest(self):
        expected = config.bundle_digest({'high': self.after, 'low': config.DEFAULT})
        with patch.object(web, 'control_status', return_value={'live': True}), patch.object(web, 'wait_curve_id', return_value=True) as wait:
            result, actual = web.apply_curve(self.after, 'high')
        wait.assert_called_once_with(expected)
        self.assertEqual(actual, expected)
        self.assertEqual(config.load(), result)

    def test_timeout_restores_file_and_waits_for_old_controller_digest(self):
        old_id = config.bundle_digest()
        with patch.object(web, 'control_status', return_value={'live': True}), patch.object(web, 'wait_curve_id', side_effect=[False, True]) as wait:
            with self.assertRaisesRegex(web.CurveUpdateError, 'restored and acknowledged'):
                web.apply_curve(self.after, 'high')
        self.assertEqual(config.load(), config.DEFAULT)
        self.assertEqual(wait.call_args.args, (old_id,))

    def test_exception_after_write_also_rolls_back(self):
        with patch.object(web, 'control_status', return_value={'live': True}), patch.object(web, 'wait_curve_id', side_effect=[OSError('status error'), True]):
            with self.assertRaisesRegex(web.CurveUpdateError, 'restored and acknowledged'):
                web.apply_curve(self.after, 'high')
        self.assertEqual(config.load(), config.DEFAULT)

    def test_save_readback_exception_after_replace_also_rolls_back(self):
        real_save = config.save
        def interrupted(record, *, profile):
            real_save(record, profile=profile)
            if record == self.after: raise OSError('readback failed after replace')
        with patch.object(web, 'save', side_effect=interrupted), patch.object(web, 'control_status', return_value={'live': True}), patch.object(web, 'wait_curve_id', return_value=True):
            with self.assertRaisesRegex(web.CurveUpdateError, 'restored and acknowledged'):
                web.apply_curve(self.after, 'high')
        self.assertEqual(config.load(), config.DEFAULT)

    def test_failed_rollback_is_reported_as_unconfirmed(self):
        with patch.object(web, 'control_status', return_value={'live': True}), patch.object(web, 'wait_curve_id', return_value=False):
            with self.assertRaisesRegex(web.CurveUpdateError, 'rollback unconfirmed'):
                web.apply_curve(self.after, 'high')

    def test_rollback_write_failure_does_not_claim_recovery(self):
        with patch.object(web, 'control_status', return_value={'live': True}), patch.object(web, 'wait_curve_id', return_value=False), patch.object(web, 'save', side_effect=[None, OSError('disk unavailable')]):
            with self.assertRaisesRegex(web.CurveUpdateError, 'rollback unconfirmed'):
                web.apply_curve(self.after, 'high')

    def test_ack_requires_live_status_and_exact_bundle(self):
        with patch.object(web, 'control_status', side_effect=[
                {'live': False, 'curve_id': 'wanted'},
                {'live': True, 'curve_id': 'old'},
                {'live': True, 'curve_id': 'wanted'}]), patch.object(web, 'time') as clock:
            clock.monotonic.side_effect = [0, 0, 1, 2]
            self.assertTrue(web.wait_curve_id('wanted'))
        self.assertEqual(clock.sleep.call_count, 2)

    def test_http_reports_unconfirmed_rollback_without_claiming_restored(self):
        h = self.handler('/api/fan/curves', dict(self.after, profile='high'))
        with patch.object(web, 'apply_curve', side_effect=web.CurveUpdateError('curve update failed; rollback unconfirmed; inspect fan controller')):
            web.Handler.do_POST(h)
        code, result = h._json.call_args.args
        self.assertEqual(code, 503)
        self.assertIn('rollback unconfirmed', result['error'])
        self.assertNotIn('restored', result['error'])
