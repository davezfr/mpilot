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

from mpilot.acquisition import *  # noqa: F403,E402

warnings.warn(
    "The app package is deprecated; use mpilot.acquisition or mpilot.api.",
    DeprecationWarning,
    stacklevel=2,
)

_TARGET = _SRC / "mpilot" / "acquisition"
if str(_TARGET) not in __path__:
    __path__.append(str(_TARGET))


def _alias_tree(old_root: str, new_root: str) -> None:
    package = import_module(new_root)
    sys.modules.setdefault(old_root, package)
    parent_name, _, child_name = old_root.rpartition(".")
    if parent_name:
        setattr(sys.modules[parent_name], child_name, package)
    for module_info in pkgutil.walk_packages(package.__path__, new_root + "."):
        module = import_module(module_info.name)
        old_name = old_root + module_info.name[len(new_root) :]
        sys.modules.setdefault(old_name, module)
        old_parent_name, _, old_child_name = old_name.rpartition(".")
        parent = sys.modules.get(old_parent_name)
        if parent is not None:
            setattr(parent, old_child_name, module)


_alias_tree("app.api", "mpilot.api")
_alias_tree("app.domain", "mpilot.acquisition.domain")
_alias_tree("app.services", "mpilot.acquisition.services")
for _old_name, _new_name in {
    "app.cli": "mpilot.acquisition.cli",
    "app.client": "mpilot.acquisition.client",
    "app.config": "mpilot.acquisition.config",
    "app.exceptions": "mpilot.acquisition.exceptions",
    "app.main": "mpilot.api.main",
    "app.models": "mpilot.acquisition.models",
}.items():
    _module = import_module(_new_name)
    sys.modules.setdefault(_old_name, _module)
    _old_parent_name, _, _old_child_name = _old_name.rpartition(".")
    _parent = sys.modules.get(_old_parent_name)
    if _parent is not None:
        setattr(_parent, _old_child_name, _module)
