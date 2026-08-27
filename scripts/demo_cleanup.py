#!/usr/bin/env python
"""Delete expired demo accounts once and print what was removed.

Locally: `make demo-sweep`. In production this is the command a scheduled job
runs, on a shorter interval than the retention sweep because demo accounts
expire on a 24-hour clock.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from ledgerai.jobs.demo_cleanup import run_demo_cleanup  # noqa: E402
from ledgerai.security.logging import install_redaction  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)-5s %(message)s")
    install_redaction()
    print(json.dumps(run_demo_cleanup(), indent=2))
