#!/usr/bin/env python
"""Delete expired demo accounts once and print what was removed.

Locally: `make demo-sweep`. In a deployed container the equivalent command is
`ledgerai-demo-cleanup`, installed on PATH by the package itself — this file
is not in the image. Both paths call the same function, so there is one
implementation and this wrapper cannot drift from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from ledgerai.cli import demo_cleanup_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(demo_cleanup_main())
