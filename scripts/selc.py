#!/usr/bin/env python3
"""Repository-local SELC command entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from selc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
