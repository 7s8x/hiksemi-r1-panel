"""Synthetic sysfs checks: no real hardware, no commands and no topology TTL."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from r1_host_sampling import HwmonSample
from r1_fan_curve import temperatures


class SamplingIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.hwmon, self.block, self.ids = [self.root / n for n in ('hwmon', 'block', 'by-id')]
        for directory in (self.hwmon, self.block, self.ids):
            directory.mkdir()
        self.cpu = self.sensor('hwmon0', 'coretemp', {'temp1_input': 51000, 'temp2_input': 57000})
        self.drive = self.sensor('hwmon1', 'drivetemp', {'temp1_input': 35000})
        self.sensor('hwmon2', 'it8613', {'fan2_input': 995, 'fan3_input': 2800,
                    'pwm2': 120, 'pwm3': 120, 'pwm2_enable': 1, 'pwm3_enable': 1})
        self.device = self.root / 'device'; self.device.mkdir()
        block = self.block / 'sda'; block.mkdir()
        (block / 'device').symlink_to(self.device, target_is_directory=True)
        (self.drive / 'device').symlink_to(self.device, target_is_directory=True)
        (self.ids / 'disk-a').symlink_to(block, target_is_directory=True)
        self.disks = (('BAY 1', 'disk-a'),)
        self.states = ('present',)
        self.site = SimpleNamespace(bay_ports=(('BAY 1', 1),), occupied_bays=('BAY 1',))

    def sensor(self, node, name, values):
        path = self.hwmon / node; path.mkdir()
        (path / 'name').write_text(name)
        for key, value in values.items(): (path / key).write_text(str(value))
        return path

    def sample(self):
        return HwmonSample(self.hwmon, block_root=self.block, by_id_root=self.ids)

    def controller_basis(self):
        return temperatures(self.hwmon, block_root=self.block, by_id_root=self.ids,
                            site=self.site, disks=self.disks)

    def test_display_and_control_basis_keep_distinct_cpu_meanings(self):
        sample = self.sample()
        self.assertEqual(sample.temperature('coretemp'), 51)
        self.assertEqual(sample.temperatures(self.disks, self.states), (57, 35, 1))
        self.assertEqual(sample.temperatures(self.disks, self.states), self.controller_basis())

    def test_one_scan_and_one_read_per_sensor_even_on_failure(self):
        reads, scans = [], []
        read, glob = Path.read_text, Path.glob
        def tracked_read(path, *args, **kwargs):
            reads.append(path)
            return read(path, *args, **kwargs)
        def tracked_glob(path, pattern):
            scans.append((path, pattern))
            return glob(path, pattern)
        with patch.object(Path, 'read_text', tracked_read), patch.object(Path, 'glob', tracked_glob):
            sample = self.sample()
            sample.temperature('coretemp'); sample.drive_temperature('disk-a'); sample.fans()
            sample.temperatures(self.disks, self.states)
            self.assertEqual(reads.count(self.cpu / 'temp1_input'), 1)
            self.assertEqual(reads.count(self.drive / 'temp1_input'), 1)
            self.assertEqual(scans.count((self.hwmon, 'hwmon*')), 1)
        (self.drive / 'temp1_input').write_text('bad')
        sample = self.sample()
        self.assertIsNone(sample.drive_temperature('disk-a'))
        (self.drive / 'temp1_input').write_text('36000')
        self.assertEqual(sample.temperatures(self.disks, self.states), (57, None, -1))
        self.assertEqual(self.sample().drive_temperature('disk-a'), 36)

    def test_topology_rebuilt_and_unrelated_values_survive_device_failure(self):
        (self.drive / 'temp1_input').write_text('bad')
        sample = self.sample()
        self.assertIsNone(sample.drive_temperature('disk-a'))
        self.assertEqual(sample.temperature('coretemp'), 51)
        self.assertEqual(sample.fans()['fan2_input'], 995)
        self.assertEqual(sample.temperatures(self.disks, self.states), self.controller_basis())
        extra = self.sensor('hwmon3', 'drivetemp', {'temp1_input': 37000})
        (extra / 'device').symlink_to(self.device, target_is_directory=True)
        self.assertEqual(self.sample().temperatures(self.disks, self.states), self.controller_basis())

    def test_bad_cpu_and_missing_or_unexpected_bay_match_safety_semantics(self):
        sample = self.sample()
        for states in (('missing',), ('unexpected',)):
            self.assertEqual(sample.temperatures(self.disks, states), (57, None, -1))
        (self.cpu / 'temp2_input').write_text('106000')
        sample = self.sample()
        self.assertEqual(sample.temperature('coretemp'), 51)
        self.assertEqual(sample.temperatures(self.disks, self.states), self.controller_basis())
        self.assertEqual(sample.temperatures(self.disks, self.states), (None, None, 0))


    def assert_same_basis(self, expected):
        actual = self.sample().temperatures(self.disks, self.states)
        self.assertEqual(actual, self.controller_basis())
        self.assertEqual(actual, expected)

    def test_cpu_temperature_inclusive_boundaries_match_controller(self):
        (self.cpu / 'temp2_input').write_text('0')
        for milli, expected in ((-1, (None, None, 0)), (0, (0, 35, 1)),
                                (105000, (105, 35, 1)), (105001, (None, None, 0))):
            with self.subTest(milli=milli):
                (self.cpu / 'temp1_input').write_text(str(milli))
                self.assert_same_basis(expected)

    def test_drive_temperature_inclusive_boundaries_match_controller(self):
        for milli, expected in ((-1, (57, None, -1)), (0, (57, 0, 1)),
                                (80000, (57, 80, 1)), (80001, (57, None, -1))):
            with self.subTest(milli=milli):
                (self.drive / 'temp1_input').write_text(str(milli))
                self.assert_same_basis(expected)

    def test_duplicate_drive_sensor_is_rejected_with_valid_temperatures(self):
        self.assert_same_basis((57, 35, 1))
        extra = self.sensor('hwmon3', 'drivetemp', {'temp1_input': 37000})
        (extra / 'device').symlink_to(self.device, target_is_directory=True)
        self.assert_same_basis((57, None, -1))

    def test_unassigned_extra_sensor_is_rejected_with_valid_temperatures(self):
        extra = self.sensor('hwmon3', 'drivetemp', {'temp1_input': 37000})
        other = self.root / 'unassigned-device'; other.mkdir()
        (extra / 'device').symlink_to(other, target_is_directory=True)
        self.assert_same_basis((57, None, -1))

    def test_missing_and_unexpected_bays_match_controller(self):
        self.disks = (('BAY 1', None),)
        self.states = ('missing',)
        self.assert_same_basis((57, None, -1))
        self.disks = (('BAY 1', 'disk-a'),)
        self.states = ('unexpected',)
        self.site.occupied_bays = ()
        self.assert_same_basis((57, None, -1))

    def test_empty_bay_is_allowed_but_orphan_sensor_is_not(self):
        self.disks = (('BAY 1', None),)
        self.states = ('empty',)
        self.site.occupied_bays = ()
        self.assert_same_basis((57, None, -1))
        # Rename outside the hwmon scan, preserving the fixture for auditability.
        self.drive.rename(self.hwmon / 'detached-drive')
        self.assert_same_basis((57, None, 0))

    def test_multiple_cpu_hwmon_devices_match_controller_failure(self):
        self.sensor('hwmon3', 'coretemp', {'temp1_input': 55000})
        self.assert_same_basis((None, None, 0))
