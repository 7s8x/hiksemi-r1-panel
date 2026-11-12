"""Pure protocol checks; the guard also prevents accidental device side effects."""
import unittest
from r1_panel_repository import validate_command

class CommandValidationTests(unittest.TestCase):
    def test_valid_commands_preserve_values(self):
        for action, value in [('page', 'off'), ('page', 'fans'), ('tap', [0, 0]),
                              ('tap', [375, 959]), ('brightness', 2), ('brightness', 100),
                              ('rgb', [2, 255, 0, 0]), ('rgb_state', None)]:
            with self.subTest(action=action, value=value):
                self.assertEqual(validate_command({'action': action, 'value': value}), (action, value))

    def test_invalid_commands_are_rejected_without_devices(self):
        for command in [None, {}, {'action': 'page', 'value': 'bad'},
                        {'action': 'page', 'value': 'fans', 'extra': 1},
                        {'action': 'tap', 'value': [376, 0]},
                        {'action': 'tap', 'value': [0, 960]},
                        {'action': 'tap', 'value': [-1, 0]},
                        {'action': 'tap', 'value': [True, 0]},
                        {'action': 'brightness', 'value': True},
                        {'action': 'brightness', 'value': 1},
                        {'action': 'brightness', 'value': 101},
                        {'action': 'rgb', 'value': [1, 2, 3]},
                        {'action': 'rgb', 'value': [1, 2, 3, False]},
                        {'action': 'rgb_state', 'value': 1},
                        {'action': 'unknown', 'value': None}]:
            with self.subTest(command=command):
                with self.assertRaises(ValueError): validate_command(command)
