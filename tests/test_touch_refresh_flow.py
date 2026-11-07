"""Exercise real touch-loop command routing using only mocked device operations."""
from contextlib import ExitStack
import copy
import json
import unittest
from unittest.mock import Mock, patch

import r1_panel_repository as panel


class TouchRefreshFlowTests(unittest.TestCase):
    def test_page_off_and_console_returns_force_refresh_without_real_devices(self):
        self.exercise_pages(cache_hit=False)

    def test_cache_hit_still_hides_cursor_without_another_snapshot_read(self):
        self.exercise_pages(cache_hit=True)

    def exercise_pages(self, *, cache_hit):
        pages = iter(('fans', 'console', 'off', 'panel', 'console', 'panel'))
        commands = [{'action': 'page', 'value': page} for page in pages]
        if cache_hit:
            commands.insert(0, {'action': 'brightness', 'value': 55})
        position = 0
        connection = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        listener = Mock()
        listener.accept.return_value = (context, None)
        def receive(_limit):
            nonlocal position
            command = commands[position]
            position += 1
            return json.dumps(command).encode()
        context.recv.side_effect = receive
        selections = 0
        def select(*args):
            nonlocal selections
            selections += 1
            if selections > len(commands) + 2:
                raise AssertionError("mock command loop failed to advance")
            if position == len(commands):
                panel.STOP = True
                return [], [], []
            return [listener], [], []
        class DevicePath:
            def __truediv__(self, name): return self
            def write_text(self, value): pass
        reader, painter, display = Mock(), Mock(), Mock()
        reader.latest.side_effect = lambda: (copy.deepcopy({'freshness': {'valid': True}}), 1)
        painter.pixels.return_value = (b'pixels', 4)
        events = []
        results = iter([True, False, True, True, True] if cache_hit else [True] * 4)
        def refresh(*args, **kwargs):
            result = next(results)
            events.append(('refresh', result))
            return result
        display.refresh.side_effect = refresh
        mapping = Mock()
        mapping.__enter__ = Mock(return_value=None)
        mapping.__exit__ = Mock(return_value=False)
        with ExitStack() as stack:
            changes = [
                patch.object(panel, 'STOP', False),
                patch.object(panel, 'Path', return_value=DevicePath()),
                patch.object(panel, 'read_text', return_value='100'),
                patch.object(panel, 'touchscreen_path', return_value='/mock/touch'),
                patch.object(panel.os, 'open', side_effect=[10, 11]),
                patch.object(panel.os, 'close'),
                patch.object(panel, 'open_control', return_value=listener),
                patch.object(panel, 'SharedReader', return_value=reader),
                patch.object(panel, 'DisplayRefresh', return_value=display),
                patch.object(panel, 'framebuffer_info', return_value=1504),
                patch.object(panel.mmap, 'mmap', return_value=mapping),
                patch.object(panel, 'write_frame'),
                patch.object(panel, 'switch_vt'),
                patch.object(panel, 'set_tty2_cursor'),
                patch.object(panel, 'publish_state'),
                patch.object(panel, 'rgb_status', return_value=False),
                patch.object(panel, 'control_status', return_value={'live': False}),
                patch.object(panel.select, 'select', side_effect=select),
                patch.object(panel.time, 'sleep'),
            ]
            mocks = [stack.enter_context(change) for change in changes]
            hide_cursor = mocks[13]
            hide_cursor.side_effect = lambda visible: events.append(('cursor', visible))
            panel.run_touch(painter, initial_view='panel')
        calls = display.refresh.call_args_list
        self.assertEqual([call.args[2] for call in calls],
                         ['panel', 'panel', 'fans', 'panel', 'panel'] if cache_hit else ['panel', 'fans', 'panel', 'panel'])
        self.assertEqual([call.kwargs['force'] for call in calls],
                         [False, False, True, True, True] if cache_hit else [False, True, True, True])
        # Startup/VT transitions also hide the cursor; verify each refresh itself.
        for index, event in enumerate(events):
            if event[0] == 'refresh':
                self.assertEqual(events[index + 1], ('cursor', False))
        if cache_hit:
            self.assertIn(('refresh', False), events)
        self.assertEqual(reader.latest.call_count, len(calls))
        reader.stop.assert_called_once()
