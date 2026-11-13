"""Shared control layout, byte projection and guarded three-button actions."""
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
import r1_panel_repository as panel
import r1_web as web
from r1_web_views import render_fans, valid_rgb_state
from r1_svg_painter import validate_svg
from ui_preview import demo_data

NS={'s':'http://www.w3.org/2000/svg'}

class ControlCardTests(unittest.TestCase):
    def data(self, mode='high', rgb=(3, 0, 130, 255)):
        data=demo_data()
        data['control']={'live':True,'mode':mode,'desired':mode,'remaining':29 if mode=='full' else None,'return_to':'low'}
        data.update(rgb=rgb[0]!=0,rgb_state=rgb)
        return data

    def test_three_equal_height_cards_and_remaining_rgb_space(self):
        root=ET.fromstring(render_fans(self.data()))
        cards=[n for n in root.findall('.//s:rect',NS) if n.get('x')=='16' and n.get('width')=='344']
        self.assertEqual([(n.get('y'),n.get('height')) for n in cards],[('99','197'),('310','197'),('521','197'),('732','211')])
        label=next(n for n in root.findall('.//s:text',NS) if n.text=='机身 RGB')
        self.assertEqual((label.get('x'),label.get('y'),label.get('font-size'),label.get('text-anchor')),('344','758','12','end'))
        text=''.join(root.itertext())
        for removed in ('温控依据','根据温度自动调节风量','已生效','已开启','已关闭'):
            self.assertNotIn(removed,text)
        for kept in ('机身 RGB','双路温控','CPU 最高温度','硬盘最高温度','全速','标准','静音','呼吸','常亮','关闭'):
            self.assertIn(kept,text)

    def test_both_button_rows_match_physical_hit_regions_and_gaps(self):
        root=ET.fromstring(render_fans(self.data()))
        for y,hit,expected in [(657,panel.fan_button_mode,('full','high','low')),(882,panel.rgb_button_action,('breathe','steady','off'))]:
            buttons=[n for n in root.findall('.//s:rect',NS) if n.get('y')==str(y)]
            self.assertEqual([(n.get('x'),n.get('width'),n.get('height')) for n in buttons],[('32','100','45'),('138','100','45'),('244','100','45')])
            for x,value in zip((32,138,244),expected):
                self.assertEqual(hit((x,y)),value)
                self.assertEqual(hit((x+100,y+45)),value)
                self.assertIsNone(hit((x,y-1)))
                self.assertIsNone(hit((x,y+46)))
            for x in (31,133,137,239,243,345):self.assertIsNone(hit((x,y)))
        self.assertIsNone(panel.fan_button_mode((80,760)))

    def test_color_is_literal_device_data_for_both_themes(self):
        for theme in ('current','mocha','latte'):
            for mode,label in ((0,'灯光已关闭'),(1,'常亮'),(2,'彩虹'),(3,'呼吸')):
                with self.subTest(theme=theme,mode=mode):
                    svg=render_fans(self.data(rgb=(mode,17,83,219)),theme=theme)
                    validate_svg(svg)
                    self.assertIn(label.encode(),svg)
                    root=ET.fromstring(svg)
                    dot=next(n for n in root.findall('.//s:circle',NS) if n.get('cy')=='801')
                    self.assertEqual(dot.get('fill-opacity'),'1' if mode in (1,3) else '0')
                    if mode in (1,3):self.assertEqual(dot.get('fill'),'#1153db')

    def test_unknown_rgb_has_no_fake_color_or_selected_effect(self):
        data=self.data();data.update(rgb=None,rgb_state=[3,'bad',0,0])
        svg=render_fans(data,theme='mocha')
        self.assertIn('灯光状态不可用'.encode(),svg)
        root=ET.fromstring(svg)
        buttons=[n for n in root.findall('.//s:rect',NS) if n.get('y')=='882']
        self.assertEqual(len({n.get('fill') for n in buttons}),1)

    def test_fan_countdown_return_target_and_missing_temperature(self):
        data=self.data('full');data['fan_basis']=(None,None,0)
        svg=render_fans(data)
        self.assertIn('全速散热 · 剩余 29 秒'.encode(),svg)
        self.assertIn('结束后返回静音模式'.encode(),svg)
        self.assertNotIn(b'None',svg)
        self.assertIn(b'--',svg)
        data['control']={'live':False}
        self.assertIn('温控服务离线'.encode(),render_fans(data))

    def test_rgb_projection_validates_all_bytes(self):
        for invalid in (None,[],[3,0,0],[4,1,2,3],[True,1,2,3],[3,-1,0,0],[3,0,0,256],[3,'red',0,0]):
            self.assertIsNone(valid_rgb_state(invalid))
        self.assertEqual(valid_rgb_state([3,0,128,255]),(3,0,128,255))

    def test_public_projection_reuses_one_ipc_read_without_hardware(self):
        with patch.object(web,'read_snapshot',return_value={}),patch.object(web,'control_status',return_value={}),patch.object(web,'request',return_value={'ok':True,'rgb':[1,12,34,56]}) as request:
            data=web.public_snapshot()
        self.assertEqual(data['rgb_state'],(1,12,34,56));self.assertTrue(data['rgb'])
        request.assert_called_once_with({'action':'rgb_state','value':None})

    def test_button_effect_preserves_current_color_and_updates_cache(self):
        for action,mode in [('breathe',3),('steady',1),('off',0)]:
            rgb=Mock();rgb.read_state.return_value=(2,12,34,56);rgb.set_state.return_value=(mode,12,34,56)
            with patch.object(panel,'RGB',rgb),patch.object(panel.Path,'exists',return_value=True),patch.object(panel,'remember_rgb_state') as cache:
                self.assertEqual(panel.select_rgb_effect(action),(mode,12,34,56))
            rgb.set_state.assert_called_once_with(mode,12,34,56)
            cache.assert_called_once_with((mode,12,34,56))

    def test_unverified_effect_buttons_cannot_write_hardware(self):
        rgb=Mock();rgb.read_state.return_value=(0,12,34,56)
        for action in ('breathe','steady'):
            with patch.object(panel,'RGB',rgb),patch.object(panel.Path,'exists',return_value=False):
                with self.assertRaises(ValueError):panel.select_rgb_effect(action)
        rgb.set_state.assert_not_called()
        with patch.object(panel,'RGB',rgb),patch.object(panel.Path,'exists',return_value=False):panel.select_rgb_effect('off')
        rgb.set_state.assert_called_once_with(0,12,34,56)

    def test_failed_hardware_write_invalidates_display_cache(self):
        rgb=Mock();rgb.read_state.return_value=(1,12,34,56);rgb.set_state.side_effect=OSError('readback failed')
        with patch.object(panel,'RGB',rgb),patch.object(panel.Path,'exists',return_value=True),patch.object(panel,'remember_rgb_state') as cache:
            with self.assertRaises(OSError):panel.select_rgb_effect('breathe')
        cache.assert_called_once_with(None)

    def test_fan_information_labels_match_control_mode_and_faults_win(self):
        for mode,label in [('high','标准'),('low','静音'),('full','全速')]:
            with self.subTest(mode=mode):
                root=ET.fromstring(render_fans(self.data(mode)))
                statuses=[n.text for n in root.findall('.//s:text',NS) if n.get('x')=='329' and n.get('y') in ('257','468')]
                self.assertEqual(statuses,[label,label])
        data=self.data('low');data['fans']['fan2_input']=0
        root=ET.fromstring(render_fans(data))
        self.assertEqual(next(n.text for n in root.findall('.//s:text',NS) if n.get('x')=='329' and n.get('y')=='257'),'停转')
