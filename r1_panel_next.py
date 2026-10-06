#!/usr/bin/env python3
"""R1 deployment adapter: keep local disk IDs out of the public code export.

The existing on-host r1_panel_buttons.py remains untouched as a rollback copy.
It supplies only its machine-specific DRIVES tuple to the sanitized panel.
"""

import r1_panel_buttons
import r1_panel_repository


r1_panel_repository.DRIVES = r1_panel_buttons.DRIVES

if __name__ == "__main__":
    r1_panel_repository.main()
