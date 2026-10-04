#!/usr/bin/env python3
"""Run from any cwd with the project venv; see ROLLOUTS.md."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlar_harness.rollout.__main__ import main

if __name__ == "__main__":
    main()
