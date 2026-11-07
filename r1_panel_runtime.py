"""Bounded background collection and a root-only control channel for the panel."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time


CONTROL_SOCKET = Path("/run/r1-panel/control.sock")
STATE_FILE = Path("/run/r1-panel/state.json")
MAX_COMMAND = 512


# Compatibility name for older panel callers.
from r1_snapshot import empty_display as empty_snapshot


class SnapshotCollector:
    def __init__(self, collect, interval: float = 15.0, observe=None, observe_snapshot=None, empty=empty_snapshot):
        self.empty = empty
        self.observe = observe
        self.observe_snapshot = observe_snapshot
        self.collect = collect
        self.interval = interval
        self._lock = threading.Lock()
        self._data = None
        self._sequence = 0
        self._sample_at = None
        self._failed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="r1-panel-collector", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=0.5)

    def latest(self):
        with self._lock:
            age = None if self._sample_at is None else max(0, time.monotonic() - self._sample_at)
            invalid = self._failed or age is None or age > self.interval * 2
            result = self.empty() if invalid else dict(self._data)
            result["freshness"] = {"age_seconds": age, "valid": not invalid,
                                   "reason": "collection_failed" if self._failed else "expired" if invalid else "fresh"}
            return result, self._sequence

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            started_ns = time.monotonic_ns()
            succeeded = False
            try:
                data = self.collect()
                with self._lock:
                    self._data = data
                    self._sample_at = time.monotonic()
                    self._failed = False
                    self._sequence += 1
                succeeded = True
                if self.observe_snapshot is not None:
                    try:
                        self.observe_snapshot(data)
                    except Exception:
                        print("snapshot_metrics_error=observation_failed", flush=True)
            except Exception:
                with self._lock:
                    self._failed = True
                    self._sequence += 1
                print("snapshot_error=collection_failed", flush=True)
            finally:
                if self.observe is not None:
                    self.observe(time.monotonic_ns() - started_ns, succeeded)
            self._stop.wait(max(0, self.interval - (time.monotonic() - started)))


def publish_state(state: str, brightness: int, path: Path = STATE_FILE):
    if state not in ("panel", "fans", "console", "off") or not 0 <= brightness <= 100:
        raise ValueError("invalid panel state")
    data = json.dumps({"state": state, "brightness": brightness,
                       "updated": time.time()}, separators=(",", ":"))
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(data)
    os.replace(temporary, path)


def read_state(path: Path = STATE_FILE, max_age: float = 5.0) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["state"] in ("panel", "fans", "console", "off")
                and isinstance(data["brightness"], int)
                and 0 <= data["brightness"] <= 100
                and 0 <= time.time() - data["updated"] <= max_age):
            return data
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def open_control(path: Path = CONTROL_SOCKET):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(4)
    listener.setblocking(False)
    return listener


def request(command: dict, path: Path = CONTROL_SOCKET, timeout: float = 2.0) -> dict:
    encoded = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_COMMAND:
        raise ValueError("command too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(encoded)
        result = client.recv(1024)
    return json.loads(result.decode("utf-8"))
