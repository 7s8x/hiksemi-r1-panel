"""Offline checks for one-minute panel timing logs."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r1_panel_metrics import MinuteMetrics, summarize  # noqa: E402


class PanelMetricsTests(unittest.TestCase):
    def test_histograms_merge_and_unavailable_fields_do_not_retain_data(self):
        metrics = MinuteMetrics()
        metrics.record("collect", 2_000_000)
        metrics.record("collect", 8_000_000)
        metrics.observe_snapshot({"cpu": None, "cpu_usage": 20, "vms": [("PRIVATE VM", "UNKNOWN")],
                                  "storage": [(("disk", "NVME", "PRIVATE SERIAL"), 40)],
                                  "bay_states": ["empty"] * 4,
                                  "storage_capacity": [(10, 90, 10)]})
        with patch("r1_panel_metrics.service_restarts", return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
            first = metrics.emit()
            second = metrics.emit()
        self.assertEqual(sum(first["latency_histograms"]["collect"]), 2)
        self.assertEqual(first["unavailable_fields"]["cpu_temperature"], 1)
        self.assertEqual(first["unavailable_fields"]["vm_state"], 1)
        self.assertEqual(first["unavailable_fields"]["storage_temperature"], 0)
        self.assertEqual(second["unavailable_fields"], {})
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_service_cpu_uses_cgroup_v2_children_inclusive_counter(self):
        from tempfile import TemporaryDirectory
        from r1_panel_metrics import cgroup_cpu_ns
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "self-cgroup").write_text("0::/system.slice/r1-panel.service\n")
            group = root / "system.slice/r1-panel.service"; group.mkdir(parents=True)
            (group / "cpu.stat").write_text("usage_usec 12345\nuser_usec 12000\n")
            def mapped(path):
                return root / "self-cgroup" if path == "/proc/self/cgroup" else root
            with patch("r1_panel_metrics.Path", side_effect=mapped):
                self.assertEqual(cgroup_cpu_ns(), 12_345_000)

    def test_percentile_uses_actual_samples_and_missing_is_null(self):
        self.assertEqual(summarize([]),
                         {"count": 0, "mean_ms": None, "p95_ms": None, "max_ms": None})
        self.assertIsNone(summarize([1_000_000])["p95_ms"])
        result = summarize([1_000_000, 2_000_000, 3_000_000, 4_000_000])
        self.assertEqual(result, {"count": 4, "mean_ms": 2.5,
                                  "p95_ms": 4.0, "max_ms": 4.0})

    def test_log_has_only_aggregate_metrics_and_resets_each_minute(self):
        metrics = MinuteMetrics()
        metrics.set_page("fans")
        metrics.observe_collect(2_000_000, True)
        metrics.record("render", 3_000_000)
        metrics.record("fb_write", 4_000_000, False)
        output = io.StringIO()
        with patch("r1_panel_metrics.service_restarts", return_value=0), \
             patch("r1_panel_metrics.process_rss_mb", return_value=25.0), \
             contextlib.redirect_stdout(output):
            metrics.emit()
            metrics.emit()
        first, second = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(first["page"], "fans")
        self.assertEqual(first["page_renders"]["fans"], 1)
        self.assertEqual(first["fb_write_errors"], 1)
        self.assertEqual(first["collect"]["mean_ms"], 2.0)
        self.assertEqual(second["collect"]["count"], 0)
        self.assertEqual(second["fb_write_errors"], 0)
        self.assertNotIn("serial", output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
