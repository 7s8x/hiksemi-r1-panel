"""Visible-content invalidation without any physical framebuffer access."""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from r1_display_refresh import DisplayRefresh
from r1_panel_metrics import MinuteMetrics


class DisplayRefreshTests(unittest.TestCase):
    def setUp(self):
        self.refresh = DisplayRefresh()
        self.data = {'clock': '12:34', 'cpu': 51, 'cpu_usage': 20,
                     'control': {'live': True, 'mode': 'full', 'desired': 'full', 'remaining': 30},
                     'rgb': True, 'freshness': {'valid': True, 'groups': {'fast': True}}}

    def test_equal_visible_content_skips_write_but_force_redraws(self):
        blit = Mock()
        with patch('r1_display_refresh.paint_svg') as paint:
            self.assertTrue(self.refresh.refresh(None, self.data, 'panel', blit))
            self.assertFalse(self.refresh.refresh(None, self.data, 'panel', blit))
            self.assertTrue(self.refresh.refresh(None, self.data, 'panel', blit, force=True))
        self.assertEqual(paint.call_count, 2)
        self.assertEqual(blit.call_count, 2)

    def test_clock_theme_page_validity_and_control_changes_invalidate(self):
        with patch('r1_display_refresh.paint_svg'), patch('r1_theme.load_theme', return_value='mocha'):
            for key, value in [('clock', '12:35'), ('cpu', 52), ('freshness', {'valid': False})]:
                self.refresh.refresh(None, self.data, 'panel', lambda: None)
                self.data[key] = value
                self.assertTrue(self.refresh.refresh(None, self.data, 'panel', lambda: None))
            self.assertTrue(self.refresh.refresh(None, self.data, 'fans', lambda: None))
            self.data['control']['remaining'] = 29
            self.assertTrue(self.refresh.refresh(None, self.data, 'fans', lambda: None))
            self.data['rgb'] = False
            self.assertTrue(self.refresh.refresh(None, self.data, 'fans', lambda: None))
        with patch('r1_display_refresh.paint_svg'), patch('r1_theme.load_theme', return_value='latte'):
            self.assertTrue(self.refresh.refresh(None, self.data, 'fans', lambda: None))

    def test_capture_timestamp_and_age_do_not_force_identical_redraw(self):
        with patch('r1_display_refresh.paint_svg'):
            self.refresh.refresh(None, self.data, 'panel', lambda: None)
            self.data.update(captured_at=200)
            self.data['freshness']['age_seconds'] = 5
            self.assertFalse(self.refresh.refresh(None, self.data, 'panel', lambda: None))

    def test_failed_blit_does_not_cache_unwritten_frame(self):
        with patch('r1_display_refresh.paint_svg'):
            with self.assertRaises(OSError):
                self.refresh.refresh(None, self.data, 'panel', Mock(side_effect=OSError()))
            self.assertTrue(self.refresh.refresh(None, self.data, 'panel', lambda: None))

    def test_cache_metrics_do_not_count_hit_as_render_and_reset(self):
        metrics = MinuteMetrics()
        with patch('r1_display_refresh.paint_svg'), patch('r1_panel_metrics.service_restarts', return_value=0), \
             contextlib.redirect_stdout(io.StringIO()):
            self.refresh.refresh(None, self.data, 'panel', lambda: None, metrics=metrics)
            self.refresh.refresh(None, self.data, 'panel', lambda: None, metrics=metrics)
            first, second = metrics.emit(), metrics.emit()
        self.assertEqual(first['render_cache_hits'], 1)
        self.assertEqual(first['prepare']['count'], 2)
        self.assertEqual(first['render']['count'], 1)
        self.assertEqual(second['render_cache_hits'], 0)
