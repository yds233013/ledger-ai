#!/usr/bin/env python
"""Run the retention sweep once and print what it did.

Locally: `make sweep`. In production this is the command a scheduled job runs.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from ledgerai.jobs.retention import run_retention_sweep  # noqa: E402
from ledgerai.security.logging import install_redaction  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)-5s %(message)s")
    install_redaction()
    print(json.dumps(run_retention_sweep(), indent=2))
