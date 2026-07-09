from __future__ import annotations

import warnings
from importlib import import_module

_MODULE = import_module("mpilot.mcp.qbitlarr_notifications")
globals().update({name: getattr(_MODULE, name) for name in dir(_MODULE) if not name.startswith("__")})

warnings.warn(
    "mcp_server.notifications is deprecated; use mpilot.mcp.qbitlarr_notifications.",
    DeprecationWarning,
    stacklevel=2,
)
