"""Internal display defaults and fast-group contract; public projection stays separate."""
import time
from r1_site_config import SITE

FAST_FIELDS = ('cpu', 'cpu_usage', 'nvme', 'drives', 'storage', 'storage_models',
               'bay_states', 'mem_total', 'mem_used', 'uptime', 'fans', 'fan_basis')

def empty_display():
    return {'cpu': None, 'cpu_usage': None, 'nvme': None, 'drives': [],
            'storage': [], 'storage_models': [], 'bay_states': [],
            'storage_usage': [None] * 5, 'storage_capacity': [None] * 5,
            'mem_total': None, 'mem_used': None, 'uptime': None, 'ip': '--',
            'vms': [(f'VM {v}', 'UNKNOWN') for v in SITE.vmids],
            'fans': None, 'fan_basis': (None, None, 0), 'control': {}, 'rgb': None,
            'clock': time.strftime('%H:%M'), 'captured_at': 0}


