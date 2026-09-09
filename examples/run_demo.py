#!/usr/bin/env python3
"""Thin CLI wrapper for a quick end-to-end demo.

Usage:
    python examples/run_demo.py
    python examples/run_demo.py --seed 7 --output /tmp/report.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import main  # noqa: E402

if __name__ == "__main__":
    main()
