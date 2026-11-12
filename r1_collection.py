"""Single display-data producer; consumers never fall back to host queries."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import signal
import threading
import time

from r1_site_config import SITE
from r1_snapshot import empty_display, FAST_FIELDS

SNAPSHOT = Path('/run/r1-collector/snapshot.json')
INTERVALS = {'fast': 5, 'vm': 60, 'nvme': 300, 'dsm': 1800, 'identity': 300}
MAX_INTERVALS = {'fast': 60, 'vm': 600, 'nvme': 3600, 'dsm': 86400, 'identity': 3600}
MAX_BYTES = 262144


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def decode_snapshot(record, *, now, boot):
    """Validate independent ages; publishing a file never renews old groups."""
    data = empty_display()
    if (not isinstance(record, dict) or record.get('schema') != 1
            or record.get('boot') != boot or not isinstance(record.get('groups'), dict)
            or not _finite(record.get('published_at'))
            or not 0 <= now - record['published_at'] <= 120):
        data['freshness'] = {'valid': False, 'reason': 'collector_unavailable', 'groups': {}}
        return data
    groups = record['groups']
    valid = {}
    for key, limit in MAX_INTERVALS.items():
        g = groups.get(key, {})
        valid[key] = (isinstance(g, dict) and g.get('valid') is True
                      and isinstance(g.get('data'), dict) and _finite(g.get('at'))
                      and _finite(g.get('interval')) and 0 < g['interval'] <= limit
                      and 0 <= now - g['at'] <= 2 * g['interval'])
    fast = groups.get('fast', {})
    fast = fast if isinstance(fast, dict) else {}
    if valid['fast']:
        data.update({k: fast['data'][k] for k in FAST_FIELDS if k in fast['data']})
        data['captured_at'] = fast['at']
    if valid['vm']:
        rows = groups['vm']['data'].get('vms', [])
        statuses = {}
        for row in rows if isinstance(rows, list) else []:
            if (isinstance(row, list) and len(row) == 2 and type(row[0]) is int
                    and row[0] in SITE.vmids and row[1] in ('RUNNING', 'STOPPED', 'UNKNOWN')):
                statuses[row[0]] = row[1] if row[0] not in statuses else 'UNKNOWN'
        data['vms'] = [(f'VM {v}', statuses.get(v, 'UNKNOWN')) for v in SITE.vmids]
    if valid['identity']:
        for k in ('pve_version', 'cpu_model'):
            value = groups['identity']['data'].get(k)
            if isinstance(value, str):
                data[k] = value[:80]
    for key, indices in (('nvme', (0,)), ('dsm', range(1, 5))):
        g = groups.get(key, {})
        g = g if isinstance(g, dict) else {}
        generation_matches = (valid['fast'] and fast.get('generation') is not None
                              and g.get('generation') == fast.get('generation'))
        valid[key] = bool(valid[key] and generation_matches)
        rows = g.get('data', {}).get('capacity', []) if valid[key] else []
        for index in indices:
            row_index = 0 if key == 'nvme' else index - 1
            row = rows[row_index] if isinstance(rows, list) and row_index < len(rows) else None
            if (isinstance(row, list) and len(row) == 3
                    and all(type(x) is int for x in row) and row[0] >= 0 and row[1] >= 0
                    and row[0] + row[1] > 0 and 0 <= row[2] <= 100):
                data['storage_capacity'][index] = row
                data['storage_usage'][index] = row[2]
    data['freshness'] = {'valid': bool(valid['fast']), 'reason': 'fresh' if valid['fast'] else 'fast_unavailable',
                         'age_seconds': max(0, now - fast['at']) if _finite(fast.get('at')) else None,
                         'groups': valid}
    return data


def read_snapshot(path=SNAPSHOT, *, now=None, boot=None):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('snapshot too large')
        record = json.loads(raw)
        return decode_snapshot(record, now=time.monotonic() if now is None else now,
                               boot=boot_id() if boot is None else boot)
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        data = empty_display()
        data['freshness'] = {'valid': False, 'reason': 'collector_unavailable', 'groups': {}}
        return data


class SharedReader:
    """SnapshotCollector-compatible reader; performs only a bounded file read."""
    def __init__(self, path=SNAPSHOT):
        self.path = path

    def start(self):
        pass

    def stop(self):
        pass

    def latest(self):
        # Use the fast capture time as the sequence, not the file's wall clock.
        data = read_snapshot(self.path)
        return data, (data.get('captured_at'), tuple(sorted(data.get('freshness', {}).get('groups', {}).items())))


def collect_fast():
    # Lazy imports keep readers independent of device/renderer initialization.
    import r1_host_sampling as panel
    from r1_storage_queries import disk_generation
    disks = panel.discover_drives()
    generation = disk_generation(disks)
    nvmes = sorted(Path('/sys/block').glob('nvme*n*'))
    nvme_generation = []
    for device in nvmes:
        nvme_generation.append((device.name, panel.read_text(device / 'diskseq'),
                                panel.read_text(device / 'size')))
    token = [generation, nvme_generation] if generation is not None else None
    sensors = panel.HwmonSample()
    states = panel.bay_states(disks)
    total, used = panel.memory()
    uptime = panel.uptime()
    nvme_temp = panel.temp_for_hwmon('nvme', sample=sensors)
    rows = [(label, panel.bay_identity(label, disk),
             panel.drive_temperature(disk, sample=sensors) if disk else None) for label, disk in disks]
    data = {'cpu': panel.temp_for_hwmon('coretemp', sample=sensors), 'cpu_usage': panel.cpu_usage(),
            'mem_total': total, 'mem_used': used, 'uptime': uptime, 'nvme': nvme_temp,
            'storage': [(panel.nvme_identity(), nvme_temp)] + [(i, t) for _, i, t in rows],
            'drives': [(label, t) for label, _, t in rows], 'fans': panel.fan_readings(sample=sensors),
            'storage_models': panel.drive_models(disks), 'bay_states': states,
            'fan_basis': sensors.temperatures(disks, states)}
    # Normalize once so in-memory generations compare exactly like JSON generations.
    return data, json.loads(json.dumps(token)), tuple(disks)


def collect_slow(key, disks):
    if key == 'vm':
        from r1_vm_status import vm_statuses
        return {'vms': list(vm_statuses().items())}
    if key == 'identity':
        from r1_host_sampling import system_identity
        version, cpu = system_identity()
        return {'pve_version': version, 'cpu_model': cpu}
    from r1_storage_queries import nvme_capacity, bay_capacities
    if key == 'nvme':
        return {'capacity': [nvme_capacity()]}
    if key != 'dsm':
        raise ValueError('unknown collection group')
    return {'capacity': bay_capacities(disks)}


class Collector:
    def __init__(self, intervals=None, *, clock=time.monotonic, fast=collect_fast,
                 slow=collect_slow, boot=None):
        self.intervals = dict(INTERVALS, **(intervals or {}))
        for key, value in self.intervals.items():
            if key not in MAX_INTERVALS or not _finite(value) or not 1 <= value <= MAX_INTERVALS[key]:
                raise ValueError('invalid collection interval')
        self.clock, self.fast, self.slow = clock, fast, slow
        self.boot = boot_id() if boot is None else boot
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.groups = {}
        self.generation = None
        self.disks = ()
        self.sequence = 0
        self.fast_ready = False
        now = clock()
        self.due = {k: now + delay for k, delay in (('vm', 0), ('identity', 5), ('nvme', 10), ('dsm', 20))}

    def sample_fast(self):
        now = self.clock()
        try:
            data, generation, disks = self.fast()
            with self.lock:
                if self.fast_ready and generation != self.generation:
                    for key in ('nvme', 'dsm'):
                        self.groups.pop(key, None)
                        self.due[key] = min(self.due[key], now + 10)
                self.generation, self.disks = generation, disks
                self.fast_ready = True
                self.groups['fast'] = {'valid': True, 'at': self.clock(), 'interval': self.intervals['fast'],
                                       'generation': generation, 'data': data}
        except Exception:
            with self.lock:
                self.groups['fast'] = {'valid': False, 'at': now, 'interval': self.intervals['fast'], 'data': {}}
            print('collect_job=fast result=failed', flush=True)

    def slow_once(self):
        with self.lock:
            if not self.fast_ready:
                return False
            now = self.clock()
            # VM has priority, but each invocation runs at most one due job.
            key = next((k for k in ('vm', 'identity', 'nvme', 'dsm') if self.due[k] <= now), None)
            if key is None:
                return False
            generation, disks = copy.deepcopy(self.generation), self.disks
            self.due[key] = float('inf')
        started = self.clock()
        try:
            value = self.slow(key, disks)
            valid = isinstance(value, dict)
        except Exception:
            value, valid = {}, False
        with self.lock:
            if key in ('nvme', 'dsm') and (generation is None or generation != self.generation):
                valid = False
            self.groups[key] = {'at': self.clock(), 'valid': valid, 'data': value,
                                'interval': self.intervals[key], 'generation': generation}
            self.due[key] = min(self.due[key], self.clock() + self.intervals[key])
        print(f'collect_job={key} result={"ok" if valid else "failed"} seconds={self.clock()-started:.3f}', flush=True)
        return True

    def export(self):
        with self.lock:
            self.sequence += 1
            return {'schema': 1, 'boot': self.boot, 'sequence': self.sequence,
                    'published_at': self.clock(), 'groups': copy.deepcopy(self.groups)}

    def publish(self, path=SNAPSHOT):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        encoded = json.dumps(self.export(), allow_nan=False, separators=(',', ':')).encode()
        if len(encoded) > MAX_BYTES:
            raise ValueError('snapshot too large')
        temporary = path.with_name(f'.{path.name}.{os.getpid()}')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def run(self, path=SNAPSHOT):
        def worker():
            while not self.stop_event.is_set():
                if not self.slow_once():
                    self.stop_event.wait(0.2)
        thread = threading.Thread(target=worker, daemon=True, name='r1-slow-collector')
        thread.start()
        try:
            while not self.stop_event.is_set():
                start = self.clock()
                self.sample_fast()
                self.publish(path)
                self.stop_event.wait(max(0, self.intervals['fast'] - (self.clock() - start)))
        finally:
            self.stop_event.set()
            thread.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('fast', 'vm', 'nvme', 'dsm'):
        parser.add_argument(f'--{key}-interval', type=int, default=INTERVALS[key])
    args = parser.parse_args()
    collector = Collector({k: getattr(args, k + '_interval') for k in ('fast', 'vm', 'nvme', 'dsm')})
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: collector.stop_event.set())
    collector.run()


if __name__ == '__main__':
    main()
