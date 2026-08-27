#!/usr/bin/env python
"""Run the retention sweep once and print what it did.

Locally: `make sweep`. In a deployed container the equivalent command is
`ledgerai-retention-sweep`, installed on PATH by the package itself — this
file is not in the image. Both paths call the same function.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from ledgerai.cli import retention_sweep_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(retention_sweep_main())
