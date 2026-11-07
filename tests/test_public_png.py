"""Lazy public image concurrency and freshness boundaries; synthetic bytes only."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import Mock, patch

from r1_public_frames import PngCache
import r1_web as web


class PublicPngTests(unittest.TestCase):
    def test_no_request_no_encoding_and_concurrent_same_page_encodes_once(self):
        entered, release = threading.Event(), threading.Event()
        def encode(svg):
            entered.set()
            self.assertTrue(release.wait(2))
            return b'PNG' + svg
        encoder = Mock(side_effect=encode)
        cache = PngCache(encoder)
        encoder.assert_not_called()
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(cache.get, 'panel', b'A') for _ in range(8)]
            try:
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
            self.assertEqual([job.result(2) for job in jobs], [b'PNGA'] * 8)
        encoder.assert_called_once_with(b'A')

    def test_only_one_entry_per_page_and_failed_encoding_does_not_relabel_old_png(self):
        encoder = Mock(side_effect=[b'1', OSError(), b'2', b'3'])
        cache = PngCache(encoder)
        self.assertEqual(cache.get('panel', b'A'), b'1')
        with self.assertRaises(OSError): cache.get('panel', b'B')
        self.assertEqual(cache.get('panel', b'B'), b'2')
        self.assertEqual(cache.get('fans', b'C'), b'3')
        self.assertEqual(len(cache.cached), 2)
        self.assertEqual(cache.cached['panel'], (b'B', b'2'))

    def frame(self, svg=b'A', **changes):
        return {'panel_svg': svg, 'updated': time.time(),
                'freshness': {'valid': True}, **changes}

    def test_cache_hits_do_not_renew_worker_or_frame_age(self):
        frame = self.frame()
        worker = Mock(); worker.latest.return_value = (frame, 1)
        cache = PngCache(lambda svg: b'PNG')
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), patch.object(web, 'PUBLIC_PNG', cache):
            self.assertEqual(web.public_image('panel'), b'PNG')
            frame['updated'] -= 46
            with self.assertRaises(RuntimeError): web.public_image('panel')
            frame['updated'] = time.time()
            frame['freshness']['valid'] = False
            with self.assertRaises(RuntimeError): web.public_image('panel')

    def test_current_collector_failure_or_group_expiry_blocks_cached_frame(self):
        frame = self.frame(groups={'fast': True, 'vm': True})
        worker = Mock(); worker.latest.return_value = (frame, 1)
        health = {'valid': True, 'groups': {'fast': True, 'vm': True}}
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), \
             patch.object(web, 'read_snapshot', side_effect=lambda: {'freshness': health}), \
             patch.object(web, 'PUBLIC_PNG', PngCache(lambda svg: b'PNG')):
            self.assertEqual(web.public_image('panel'), b'PNG')
            health['groups']['vm'] = False
            with self.assertRaises(RuntimeError): web.public_image('panel')
            health['valid'] = False
            with self.assertRaises(RuntimeError): web.public_image('panel', svg=True)

    def test_failed_during_encoding_never_returns_encoded_old_frame(self):
        frame = self.frame()
        worker = Mock(); worker.latest.return_value = (frame, 1)
        def encode(svg):
            frame['freshness']['valid'] = False
            return b'old PNG'
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), \
             patch.object(web, 'PUBLIC_PNG', PngCache(encode)):
            with self.assertRaises(RuntimeError): web.public_image('panel')

    def test_frame_changes_during_encoding_retries_latest_svg(self):
        frame = self.frame()
        worker = Mock(); worker.latest.return_value = (frame, 1)
        def encode(svg):
            frame['panel_svg'] = b'B'
            return b'PNG' + svg
        cache = PngCache(encode)
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), patch.object(web, 'PUBLIC_PNG', cache):
            self.assertEqual(web.public_image('panel'), b'PNGB')

    def test_public_worker_fails_closed_when_shared_fast_data_invalid(self):
        with patch.object(web, 'public_snapshot', return_value={'freshness': {'valid': False}}):
            with self.assertRaises(RuntimeError): web.collect_public_frames()

    def test_no_rasterization_for_svg_request(self):
        worker = Mock(); worker.latest.return_value = (self.frame(), 1)
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), patch.object(web.PUBLIC_PNG, 'get') as encode:
            self.assertEqual(web.public_image('panel', svg=True), b'A')
        encode.assert_not_called()
