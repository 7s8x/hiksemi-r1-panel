"""Cache behavior for snapshot-time external commands and hwmon discovery."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import r1_panel_repository as repo  # noqa: E402


class SnapshotCacheTests(unittest.TestCase):
    def test_network_address_caches_within_ttl(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            class R:
                stdout = "192.168.50.3/24 scope global vmbr0\n"
            return R()

        with patch.object(repo.subprocess, "run", fake_run):
            first = repo.network_address()
            second = repo.network_address()
        self.assertEqual(first, "192.168.50.3")
        self.assertEqual(second, "192.168.50.3")
        self.assertEqual(len(calls), 1, "second call should hit cache, not re-run `ip`")

    def test_vm_status_caches_per_vmid_within_ttl(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(tuple(args))
            class R:
                stdout = "status: running\n"
            return R()

        with patch.object(repo.subprocess, "run", fake_run):
            a1 = repo.vm_status(100)
            a2 = repo.vm_status(100)
            b1 = repo.vm_status(111)
        self.assertEqual(a1, "RUNNING")
        self.assertEqual(a2, "RUNNING")
        self.assertEqual(b1, "RUNNING")
        # 100 is cached (1 spawn); 111 is a different VM (1 spawn) => 2 total.
        self.assertEqual(len(calls), 2)
        self.assertIn(("qm", "status", "100"), calls)
        self.assertIn(("qm", "status", "111"), calls)


if __name__ == "__main__":
    unittest.main()
