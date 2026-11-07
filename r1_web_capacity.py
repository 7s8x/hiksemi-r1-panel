"""Compatibility exports for capacity callers; implementation is host-side."""
from r1_storage_queries import nvme_capacity as _nvme_capacity, guest_capacity as _guest_capacity

# The facade exports are intentional, including legacy private helper names.
__all__ = ['_nvme_capacity', '_guest_capacity']
