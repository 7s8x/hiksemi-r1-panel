"""Incremental DOM must reproduce approved SVGs without exposing raw snapshots."""
import json
import time
import threading
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
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
    def test_information_header_keeps_only_larger_uptime_below_title(self):
        data = {**demo_data(), 'pve_version': 'PROXMOX VE 9.2.26',
                'uptime': 3 * 86400 + 17 * 3600, 'clock': '12:13'}
        values = information_values(data)
        self.assertNotIn('PVE_VERSION', values)
        for theme in ('mocha', 'latte'):
            direct = ET.fromstring(render_information(data, theme=theme))
            self.assertEqual(canonical(direct), canonical(applied('panel', values, theme)))
            texts = {node.text: node for node in direct.iter() if node.tag.endswith('}text')}
            self.assertNotIn(data['pve_version'], texts)
            uptime = texts['运行 3 天 17 小时']
            self.assertEqual((uptime.get('x'), uptime.get('y'), uptime.get('font-size')), ('20', '74', '18'))
            self.assertEqual(texts['12:13'].get('y'), '80')
            self.assertIn('在线', texts)
            fan_data = fan_values(data)
            self.assertNotIn('PVE_VERSION', fan_data)
            fans = ET.fromstring(render_fans(data, theme=theme))
            self.assertEqual(canonical(fans), canonical(applied('fans', fan_data, theme)))
            fan_header = {n.text: n.attrib for n in fans.iter() if n.tag.endswith('}text')
                          and n.get('y') and float(n.get('y')) < 99}
            self.assertEqual(fan_header, {key: node.attrib for key, node in texts.items()
                                          if node.get('y') and float(node.get('y')) < 99})

    def test_information_card_larger_readings_and_no_drive_model(self):
        for theme in ('mocha', 'latte'):
            data = {**demo_data(), 'storage_models': ['hidden-model'] * 5}
            values = information_values(data)
            self.assertEqual(values['MEM_FONT'], 25)
            self.assertEqual((values['D1_TITLE'], values['D1_DETAIL']), ('BAY 1', '2TB SSD'))
            svg = render_information(data, theme=theme)
            self.assertNotIn(b'hidden-model', svg)
            elements = list(ET.fromstring(svg).iter())
            by_text = {n.text: n for n in elements if n.tag.endswith('}text') and n.text}
            used = next(n for n in elements if n.get('x') == '42' and n.get('y') == '564')
            self.assertEqual(by_text['BAY 1'].get('font-size'), used.get('font-size'))
            self.assertEqual(by_text['2TB SSD'].get('font-size'), '30')
            self.assertEqual(canonical(ET.fromstring(svg)), canonical(applied('panel', values, theme)))
            values = information_values({**data, 'mem_used': 9999.9, 'mem_total': 10000})
            self.assertLess(values['MEM_FONT'], 25)

    def test_cpu_and_fan_rings_use_separate_load_thresholds(self):
        cool, warm, hot = '#74bffe', '#f4b46f', '#f38ba8'
        for percent, expected in ((59, cool), (60, warm), (84, warm), (85, hot)):
            data = {**demo_data(), 'cpu_usage': percent}
            self.assertEqual(information_values(data)['CPU_ARC_COLOR'], expected)
            self.assertEqual(canonical(ET.fromstring(render_information(data, theme='mocha'))),
                             canonical(applied('panel', information_values(data), 'mocha')))
        for pwm, expected in ((176, cool), (179, warm), (226, warm), (230, hot), (255, hot)):
            data = {**demo_data(), 'fans': {'pwm2': pwm, 'fan2_input': 1000, 'pwm3': 255, 'fan3_input': 0}}
            values = fan_values(data)
            self.assertEqual(values['F2_ARC_COLOR'], expected)
            self.assertEqual(values['F3_ARC_COLOR'], warm)
            self.assertEqual(values['F3_STATUS'], '停转')
            self.assertEqual(canonical(ET.fromstring(render_fans(data, theme='latte'))),
                             canonical(applied('fans', values, 'latte')))

    def test_storage_capacity_bar_thresholds_and_theme_projection(self):
        for theme, colors in (
                ('mocha', ['#89b4fa', '#fab387', '#fab387', '#f38ba8']),
                ('latte', ['#1e66f5', '#fe640b', '#fe640b', '#d20f39'])):
            with self.subTest(theme=theme):
                data = demo_data()
                data.update(storage_usage=[74, 75, 89, 90, None], storage_capacity=[])
                values = information_values(data)
                direct = ET.fromstring(render_information(data, theme=theme))
                self.assertEqual(canonical(direct), canonical(applied('panel', values, theme)))
                bars = [n for n in direct.iter() if n.tag.endswith('}rect')
                        and n.get('x') == '42' and n.get('height') == '6']
                fills = bars[1::2]
                self.assertEqual(len(fills), 5)
                self.assertEqual([n.get('fill') for n in fills[:4]], colors)
                self.assertEqual(fills[4].get('width'), '0')

    def test_vm_selection_http_auth_csrf_and_persistence(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/vm-monitor'
        admin = {'Cookie': 'r1session=session', 'Origin': url.rsplit('/api/', 1)[0]}
        write = {**admin, 'X-R1-CSRF': 'csrf', 'Content-Type': 'application/json'}

        def send(headers, body=None):
            request = Request(url, headers=headers, data=json.dumps(body).encode() if body is not None else None)
            with urlopen(request, timeout=2) as response:
                return json.load(response)

        def denied(code, headers, body=None):
            with self.assertRaises(HTTPError) as error:
                send(headers, body)
            self.assertEqual(error.exception.code, code)
            error.exception.close()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'vm-monitor.json'
                with patch.object(web.vm_monitor, 'SELECTION_FILE', path), \
                     patch.object(web, 'LOG_ENABLED', False), \
                     patch.object(web, 'read_snapshot', return_value={'vm_inventory': [100, 111, 200]}) as read, \
                     patch.dict(web.SESSIONS, {'session': (time.time()+60, 'csrf')}, clear=True):
                    denied(401, {})
                    self.assertEqual(send(admin)['vmids'], [100, 111, 200])
                    denied(403, admin, {'vmids': [100, 200]})
                    denied(400, write, {'vmids': [True]})
                    denied(400, write, {'vmids': [999]})
                    self.assertFalse(path.exists())
                    self.assertEqual(send(write, {'vmids': [100, 200]})['selected'], [100, 200])
                    self.assertEqual(send(admin)['selected'], [100, 200])
                    labels = {'100': {'mode': 'pve'}, '200': {'mode': 'id'}}
                    self.assertEqual(send(write, {'vmids': [100, 200], 'labels': labels})['labels'], labels)
                    denied(400, write, {'vmids': [100, 200], 'labels': {'200': {'mode': 'custom', 'name': '家庭助手'}}})
                    self.assertEqual(send(admin)['labels'], labels)
                    read.return_value = {}
                    denied(503, write, {'vmids': []})
                    self.assertEqual(json.loads(path.read_text())['vmids'], [100, 200])
                    read.return_value = {'vm_inventory': [100, 111, 200]}
                    self.assertEqual(send(write, {'vmids': []})['selected'], [])
                    self.assertEqual(send(admin)['selected'], [])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_projection_shared_by_clients_but_health_checked_once_each_request(self):
        frame = {'values': {'panel': information_values({}), 'fans': fan_values({})},
                 'theme': 'mocha', 'groups': {}, 'updated': 100.0}
        worker = Mock(); worker.latest.return_value = (frame, 1)
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), \
             patch.object(web, 'PUBLIC_DOCUMENT_CACHE', None), \
             patch.object(web, 'read_snapshot', return_value={'freshness': {'valid': True, 'groups': {}}}) as health, \
             patch.object(web, 'project', wraps=web.project) as project_mock, \
             patch.object(web.time, 'time', return_value=120.0):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: web.public_document(), range(8)))
            self.assertEqual(health.call_count, 8)
            self.assertEqual(project_mock.call_count, 2, 'one projection per page, not per client')
            self.assertTrue(all(r == results[0] for r in results))
            with patch.object(web.time, 'time', return_value=140.0):
                self.assertEqual(web.public_document()['valid_for'], 5)
            self.assertEqual(project_mock.call_count, 2, 'cache must not preserve old TTL')
            health.return_value = {'freshness': {'valid': False}}
            with self.assertRaises(RuntimeError): web.public_document()
            self.assertEqual(project_mock.call_count, 2)
            health.return_value = {'freshness': {'valid': True, 'groups': {}}}
            frame['updated'] = 101.0
            web.public_document()
            self.assertEqual(project_mock.call_count, 4)
            frame['theme'] = 'latte'
            web.public_document()
            self.assertEqual(project_mock.call_count, 6, 'layout change invalidates even at same timestamp')

    def test_worker_failure_during_projection_does_not_return_cached_document(self):
        frame = {'values': {'panel': information_values({}), 'fans': fan_values({})},
                 'theme': 'mocha', 'groups': {}, 'updated': time.time()}
        worker = Mock(); worker.latest.side_effect = [(frame, 1), ({'freshness': {'valid': False}}, 2)]
        with patch.object(web, 'PUBLIC_COLLECTOR', worker), \
             patch.object(web, 'PUBLIC_DOCUMENT_CACHE', None), \
             patch.object(web, 'read_snapshot', return_value={'freshness': {'valid': True, 'groups': {}}}) as health:
            with self.assertRaises(RuntimeError): web.public_document()
            health.assert_called_once()

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
                frame['values']['fans'] = fan_values({'vms':[('VM 100','RUNNING')]})
                frame['admin_fans'] = fan_values({'vms':[('VM 100','RUNNING')], 'vm_display_names':{'VM 100':'家庭助手'}})
                private_url = f'http://127.0.0.1:{server.server_port}/api/display-state'
                with self.assertRaises(HTTPError) as denied:
                    urlopen(private_url, timeout=2)
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()
                with patch.dict(web.SESSIONS, {'session':(time.time()+60, 'csrf')}):
                    with urlopen(Request(private_url, headers={'Cookie':'r1session=session'}), timeout=2) as response:
                        self.assertIn('家庭助手',json.load(response)['pages']['fans'])
                for endpoint in ('state', 'layout'):
                    url = f'http://127.0.0.1:{server.server_port}/public/{endpoint}.json'
                    with urlopen(url, timeout=2) as response:
                        self.assertEqual(response.headers.get_content_type(), 'application/json')
                        self.assertIn('no-store', response.headers['Cache-Control'])
                        document = json.load(response)
                        self.assertEqual(document['theme'], 'mocha')
                        if endpoint == 'state':
                            self.assertNotIn('家庭助手', document['pages']['fans'])
                            self.assertIn('VM 100', document['pages']['fans'])
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
        memory = next(n for n in root.iter() if n.get('x') == '329' and n.get('y') == '260')
        self.assertEqual(len(memory), 1)
        self.assertEqual(memory.text, '--')
        suffix = memory[0]
        self.assertEqual(suffix.text, ' / 15 GiB')
        self.assertLess(int(suffix.get('font-size')), int(memory.get('font-size')))
        for attr in ('y', 'dy', 'dominant-baseline', 'baseline-shift'):
            self.assertIsNone(suffix.get(attr))

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
