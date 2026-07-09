"""Compatibility namespace for the former package name.

New code should import :mod:`mpilot.subtitles`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import mpilot.subtitles as _subtitles

__path__ = _subtitles.__path__  # type: ignore[attr-defined]
