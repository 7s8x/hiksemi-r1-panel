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


def empty_snapshot() -> dict:
    return {"cpu": None, "nvme": None, "drives": [], "mem_total": 0,
            "mem_used": 0, "uptime": 0, "ip": "--",
            "vms": [(100, "UNKNOWN"), (111, "UNKNOWN")], "fans": None,
            "control": {}, "rgb": None, "captured_at": 0,
            "clock": time.strftime("%H:%M")}


class SnapshotCollector:
    def __init__(self, collect, interval: float = 15.0):
        self.collect = collect
        self.interval = interval
        self._lock = threading.Lock()
        self._data = None
        self._sequence = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="r1-panel-collector", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=0.5)

    def latest(self):
        with self._lock:
            return (dict(self._data) if self._data is not None else empty_snapshot(),
                    self._sequence)

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                data = self.collect()
                with self._lock:
                    self._data = data
                    self._sequence += 1
            except Exception as error:
                print(f"snapshot_error={error}", flush=True)
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
