from __future__ import annotations

import warnings
from importlib import import_module

_MODULE = import_module("mpilot.api.main")
globals().update({name: getattr(_MODULE, name) for name in dir(_MODULE) if not name.startswith("__")})

warnings.warn(
    "app.main is deprecated; use mpilot.api.main.",
    DeprecationWarning,
    stacklevel=2,
)
