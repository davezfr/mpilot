from __future__ import annotations

import warnings
from pathlib import Path

from mpilot.api import *  # noqa: F403

warnings.warn(
    "app.api is deprecated; use mpilot.api.",
    DeprecationWarning,
    stacklevel=2,
)

_TARGET = Path(__file__).resolve().parents[2] / "src" / "mpilot" / "api"
if str(_TARGET) not in __path__:
    __path__.append(str(_TARGET))
