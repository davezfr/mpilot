from __future__ import annotations

import warnings
import pkgutil
import sys
from importlib import import_module
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mpilot.runtime import *  # noqa: F403,E402

warnings.warn(
    "The media_workflow_runtime package is deprecated; use mpilot.runtime.",
    DeprecationWarning,
    stacklevel=2,
)

_TARGET = _SRC / "mpilot" / "runtime"
if str(_TARGET) not in __path__:
    __path__.append(str(_TARGET))

_PACKAGE = import_module("mpilot.runtime")
for _module_info in pkgutil.walk_packages(_PACKAGE.__path__, "mpilot.runtime."):
    if _module_info.name == "mpilot.runtime.__main__":
        continue
    _module = import_module(_module_info.name)
    _old_name = "media_workflow_runtime" + _module_info.name[len("mpilot.runtime") :]
    sys.modules.setdefault(_old_name, _module)
    _old_parent_name, _, _old_child_name = _old_name.rpartition(".")
    _parent = sys.modules.get(_old_parent_name)
    if _parent is not None:
        setattr(_parent, _old_child_name, _module)
