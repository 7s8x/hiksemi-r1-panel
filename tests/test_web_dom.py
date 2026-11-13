"""Incremental DOM must reproduce approved SVGs without exposing raw snapshots."""
import json
import time
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from r1_web_dom import document_layout, layout, project
from r1_web_views import information_values, fan_values, render_information, render_fans
import r1_web as web
from ui_preview import demo_data


def applied(view, values, theme):
    spec, _ = layout(view, theme)
    root = ET.fromstring(spec['svg'])
    nodes = {n.get('id'): n for n in root.iter() if n.get('id')}
    for (identifier, attr), value in zip(spec['bindings'], project(view, values, theme)):
        if attr == 'text':
            nodes[identifier].text = value
        else:
            nodes[identifier].set(attr, value)
    return root


def canonical(root):
    return [(n.tag, {k: v for k, v in n.attrib.items() if k != 'id'},
             (n.text or '').strip(), (n.tail or '').strip()) for n in root.iter()]


class WebDomTests(unittest.TestCase):
    def test_http_protocol_preserves_public_switch_auth_and_expiry(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        frame = {'values': {'panel': information_values({}), 'fans': fan_values({})},
                 'theme': 'mocha', 'updated': time.time()}
        worker = Mock(); worker.latest.return_value = (frame, 1)
        try:
            with patch.object(web, 'PUBLIC_COLLECTOR', worker), patch.object(web, 'LOG_ENABLED', False), \
                 patch.dict(web.SESSIONS, {}, clear=True), patch.object(web, 'PUBLIC_READONLY', True):
                for endpoint in ('state', 'layout'):
                    url = f'http://127.0.0.1:{server.server_port}/public/{endpoint}.json'
                    with urlopen(url, timeout=2) as response:
                        self.assertEqual(response.headers.get_content_type(), 'application/json')
                        self.assertIn('no-store', response.headers['Cache-Control'])
                        self.assertEqual(json.load(response)['theme'], 'mocha')
                    with patch.object(web, 'PUBLIC_READONLY', False):
                        with self.assertRaises(HTTPError) as error: urlopen(url, timeout=2)
                        self.assertEqual(error.exception.code, 401)
                    with patch.object(web, 'client_allowed', return_value=False):
                        with self.assertRaises(HTTPError) as error: urlopen(url, timeout=2)
                        self.assertEqual(error.exception.code, 403)
                    frame['updated'] -= 46
                    with self.assertRaises(HTTPError) as error: urlopen(url, timeout=2)
                    self.assertEqual(error.exception.code, 503)
                    frame['updated'] = time.time()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_dom_matches_existing_render_in_all_themes_and_states(self):
        for theme in ('current', 'mocha', 'latte'):
            for mode in ('high', 'low', 'full', 'offline', 'switching'):
                for rgb in (None, (0, 0, 0, 0), (1, 107, 191, 229), (3, 171, 0, 255)):
                    data = demo_data()
                    data['control'] = {'live': mode != 'offline', 'mode': mode,
                                       'desired': 'low' if mode == 'switching' else mode,
                                       'remaining': 29, 'return_to': 'low'}
                    data['rgb_state'] = rgb
                    for view, values, render in (('panel', information_values, render_information),
                                                  ('fans', fan_values, render_fans)):
                        with self.subTest(theme=theme, mode=mode, rgb=rgb, view=view):
                            self.assertEqual(canonical(applied(view, values(data), theme)),
                                             canonical(ET.fromstring(render(data, theme=theme))))

    def test_missing_and_hostile_text_preserve_tspans_and_never_become_markup(self):
        data = {'cpu_model': '<script>alert(1)</script>', 'mem_used': None,
                'mem_total': 15, 'cpu': None, 'storage': [], 'fans': {}}
        root = applied('panel', information_values(data), 'mocha')
        self.assertEqual(canonical(root), canonical(ET.fromstring(render_information(data, theme='mocha'))))
        self.assertFalse(any(n.tag.endswith('script') for n in root.iter()))
        memory = next(n for n in root.iter() if n.get('x') == '329' and n.get('y') == '224')
        self.assertEqual(len(memory), 2)
        self.assertEqual(memory.text, '--')

    def test_worker_formats_only_and_compatibility_svg_is_lazy(self):
        with patch.object(web, 'public_snapshot', return_value=demo_data()), \
             patch.object(web, 'load_theme', return_value='mocha'), \
             patch.object(web, 'render_values', wraps=web.render_values) as render:
            frame = web.collect_public_frames()
            render.assert_not_called()
            worker = Mock(); worker.latest.return_value = (frame, 1)
            with patch.object(web, 'PUBLIC_COLLECTOR', worker), \
                 patch.object(web, 'read_snapshot', return_value={'freshness': {'valid': True, 'groups': {}}}):
                result = web.public_document()
                self.assertEqual(result['id'], document_layout('mocha')['id'])
                self.assertTrue(0 < result['valid_for'] <= 30)
                render.assert_not_called()
                self.assertIn(b'<', web.public_image('panel', svg=True))
                self.assertEqual(render.call_count, 1)
                web.public_image('panel', svg=True)
                self.assertEqual(render.call_count, 1)

    def test_expiry_group_failure_and_theme_change_invalidate_protocol(self):
        frame = {'values': {'panel': information_values({}), 'fans': fan_values({})},
                 'theme': 'mocha', 'groups': {}, 'updated': time.time()}
        worker = Mock(); worker.latest.return_value = (frame, 1)
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), \
             patch.object(web, 'read_snapshot', return_value={'freshness': {'valid': True, 'groups': {}}}):
            first = web.public_document()
            frame['theme'] = 'latte'
            self.assertNotEqual(first['id'], web.public_document()['id'])
            frame['updated'] -= 46
            for include in (False, True):
                with self.assertRaises(RuntimeError): web.public_document(include)
            frame['updated'] = time.time()
            frame['groups'] = {'dsm': False}
            with self.assertRaises(RuntimeError): web.public_document()

    def test_protocol_only_projects_display_slots(self):
        values = information_values(demo_data())
        values['SECRET'] = 'should never escape'
        self.assertNotIn('should never escape', json.dumps(project('panel', values, 'mocha')))
