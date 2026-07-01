"""Гарантируем, что корень проекта в sys.path для импорта пакета arb и tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
