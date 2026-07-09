from __future__ import annotations

import warnings
from importlib import import_module

_MODULE = import_module("mpilot.mcp.legacy_qbitlarr")
globals().update({name: getattr(_MODULE, name) for name in dir(_MODULE) if not name.startswith("__")})

warnings.warn(
    "mcp_server.server is deprecated; use mpilot.mcp.legacy_qbitlarr.",
    DeprecationWarning,
    stacklevel=2,
)
