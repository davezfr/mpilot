from __future__ import annotations

import warnings
import sys
from importlib import import_module
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

warnings.warn(
    "The mcp_server package is deprecated; use mpilot.mcp.",
    DeprecationWarning,
    stacklevel=2,
)

server = import_module("mpilot.mcp.legacy_qbitlarr")
notifications = import_module("mpilot.mcp.qbitlarr_notifications")

sys.modules.setdefault("mcp_server.server", server)
sys.modules.setdefault("mcp_server.notifications", notifications)
