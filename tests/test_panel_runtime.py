"""Offline tests for collection isolation and command validation helpers."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_panel_runtime import SnapshotCollector, publish_state, read_state  # noqa: E402
import r1_panel_repository  # noqa: E402


class PanelRuntimeTests(unittest.TestCase):

    def test_slow_collection_does_not_block_snapshot_read(self):
        gate = threading.Event()
        def collect():
            gate.wait(0.5)
            return {"cpu": 65}
        collector = SnapshotCollector(collect, interval=0.1)
        collector.start()
        started = time.monotonic()
        first, sequence = collector.latest()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertIsNone(first["cpu"])
        self.assertEqual(sequence, 0)
        gate.set()
        time.sleep(0.02)
        latest, sequence = collector.latest()
        self.assertGreaterEqual(sequence, 1)
        self.assertEqual(latest["cpu"], 65)
        collector.stop()

    def test_state_is_atomically_read_and_staleness_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            publish_state("fans", 45, path)
            self.assertEqual(read_state(path)["state"], "fans")
            self.assertIsNone(read_state(path, max_age=-1))

    def test_only_tty2_cursor_is_hidden_and_restored(self):
        with patch.object(r1_panel_repository.os, "open", return_value=7) as opened, \
             patch.object(r1_panel_repository.os, "write") as written, \
             patch.object(r1_panel_repository.os, "close") as closed:
            r1_panel_repository.set_tty2_cursor(False)
            r1_panel_repository.set_tty2_cursor(True)
        self.assertEqual(opened.call_args_list[0].args[0], "/dev/tty2")
        self.assertEqual([call.args[1] for call in written.call_args_list],
                         [b"\x1b[?25l", b"\x1b[?25h"])
        self.assertEqual(closed.call_count, 2)



class PayloadDefaultsTests(unittest.TestCase):
    def test_public_worker_uses_empty_frames_not_panel_defaults(self):
        from r1_panel_runtime import SnapshotCollector
        collector = SnapshotCollector(lambda: {}, empty=dict)
        frames, _ = collector.latest()
        self.assertNotIn('cpu', frames)
        self.assertNotIn('updated', frames)
        self.assertFalse(frames['freshness']['valid'])

    def test_rgb_control_does_not_import_trial_or_probe(self):
        import ast
        root = Path(__file__).resolve().parents[1]
        for name in ('r1_rgb_control.py', 'r1_rgb_io.py'):
            imports = [node.module for node in ast.walk(ast.parse((root / name).read_text()))
                       if isinstance(node, ast.ImportFrom)]
            self.assertNotIn('r1_rgb_trial', imports)
            self.assertNotIn('r1_rgb_probe', imports)

if __name__ == "__main__":
    unittest.main()
