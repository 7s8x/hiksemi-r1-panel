"""Optional, low-volume performance counters for the physical R1 panel.

Only operation timings and process resource use are logged. No snapshot values,
disk identities, device paths, or control commands are retained here.
"""

from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time


OPERATIONS = ("collect", "prepare", "render", "fb_write")
HISTOGRAM_BOUNDS_MS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000)


def cgroup_cpu_ns():
    """Cumulative CPU of this service, including child commands, on cgroup v2."""
    try:
        path = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
        root = Path("/sys/fs/cgroup")
        group = (root / path.lstrip("/")).resolve()
        if root not in group.parents or group.name != "r1-panel.service":
            return None
        values = dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())
        return int(values["usage_usec"]) * 1000
    except (OSError, ValueError, KeyError, StopIteration):
        return None


def summarize(samples: list[int]) -> dict:
    """Return measured milliseconds; a single sample has no useful P95."""
    if not samples:
        return {"count": 0, "mean_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "mean_ms": round(sum(ordered) / len(ordered) / 1_000_000, 3),
        "p95_ms": (round(ordered[math.ceil(0.95 * len(ordered)) - 1] / 1_000_000, 3)
                   if len(ordered) >= 2 else None),
        "max_ms": round(ordered[-1] / 1_000_000, 3),
    }


def process_rss_mb() -> float | None:
    try:
        resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        return round(resident_pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 2)
    except (OSError, ValueError, IndexError):
        return None


def service_restarts() -> int | None:
    try:
        result = subprocess.run(
            ["systemctl", "show", "r1-panel.service", "-p", "NRestarts", "--value"],
            check=True, capture_output=True, text=True, timeout=0.5,
        )
        return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class MinuteMetrics:
    def __init__(self, interval: float = 60.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._samples = {name: [] for name in OPERATIONS}
        self._errors = {name: 0 for name in OPERATIONS}
        self._histograms = {name: [0] * (len(HISTOGRAM_BOUNDS_MS) + 1) for name in OPERATIONS}
        self._cache_hits = 0
        self._unavailable = {}
        self._page = "panel"
        self._page_counts = {page: 0 for page in ("panel", "fans", "console", "off")}
        self._cpu_start = time.process_time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="r1-panel-metrics", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1)
        self.emit()

    def set_page(self, page: str):
        if page not in self._page_counts:
            raise ValueError("unknown panel page")
        with self._lock:
            self._page = page

    def record(self, operation: str, elapsed_ns: int, succeeded: bool = True):
        if operation not in OPERATIONS:
            raise ValueError("unknown panel operation")
        with self._lock:
            self._samples[operation].append(max(0, elapsed_ns))
            bucket = next((i for i, bound in enumerate(HISTOGRAM_BOUNDS_MS)
                           if elapsed_ns <= bound * 1_000_000), len(HISTOGRAM_BOUNDS_MS))
            self._histograms[operation][bucket] += 1
            if not succeeded:
                self._errors[operation] += 1
            if operation == "render":
                self._page_counts[self._page] += 1

    def cache_hit(self):
        with self._lock:
            self._cache_hits += 1

    def observe_collect(self, elapsed_ns: int, succeeded: bool):
        self.record("collect", elapsed_ns, succeeded)

    def observe_snapshot(self, data):
        """Count unavailable fields only; never retain values or identifiers."""
        storage = list(data.get("storage") or [])
        states = ["present"] + list(data.get("bay_states") or ["present"] * 4)
        counts = {
            "cpu_temperature": int(data.get("cpu") is None),
            "cpu_usage": int(data.get("cpu_usage") is None),
            "fan_reading": int(data.get("fans") is None),
            "vm_state": sum(state not in ("RUNNING", "STOPPED") for _, state in data.get("vms", [])),
            "storage_temperature": sum((i >= len(storage) or storage[i][1] is None)
                                       for i, state in enumerate(states) if state != "empty"),
            "storage_capacity": sum((i >= len(data.get("storage_capacity") or []) or
                                      data["storage_capacity"][i] is None)
                                     for i, state in enumerate(states) if state != "empty"),
        }
        with self._lock:
            for name, count in counts.items():
                self._unavailable[name] = self._unavailable.get(name, 0) + count

    def emit(self):
        with self._lock:
            cache_hits, self._cache_hits = self._cache_hits, 0
            samples = self._samples
            errors = self._errors
            page = self._page
            page_counts = self._page_counts
            histograms, unavailable = self._histograms, self._unavailable
            self._samples = {name: [] for name in OPERATIONS}
            self._errors = {name: 0 for name in OPERATIONS}
            self._histograms = {name: [0] * (len(HISTOGRAM_BOUNDS_MS) + 1) for name in OPERATIONS}
            self._unavailable = {}
            self._page_counts = dict.fromkeys(page_counts, 0)
        cpu_now = time.process_time()
        cpu_delta = round(max(0, cpu_now - self._cpu_start), 4)
        self._cpu_start = cpu_now
        payload = {
            "event": "r1_panel_metrics",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "metrics_schema": 1,
            "histogram_bounds_ms": HISTOGRAM_BOUNDS_MS,
            "latency_histograms": histograms,
            "unavailable_fields": unavailable,
            "page": page,
            "page_renders": page_counts,
            "render_cache_hits": cache_hits,
            **{name: summarize(samples[name]) for name in OPERATIONS},
            **{name + "_errors": errors[name] for name in OPERATIONS},
            "process_cpu_seconds_delta": cpu_delta,
            "service_cpu_usage_ns": cgroup_cpu_ns(),
            "rss_mb": process_rss_mb(),
            "service_restarts": service_restarts(),
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
        return payload

    def _run(self):
        while not self._stop.wait(self.interval):
            self.emit()
