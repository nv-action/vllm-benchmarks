"""Make the repository's script-style framework modules importable in tests."""

from __future__ import annotations

import sys
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK))
