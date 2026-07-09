from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional


def load_project_dotenv(start: Optional[Path] = None) -> Optional[Path]:
    if os.environ.get("BABELARR_NO_DOTENV") or os.environ.get("MST_NO_DOTENV"):
        return None
    path = find_dotenv(start or Path.cwd())
    if path is None:
        return None
    values = parse_dotenv(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return path


def find_dotenv(start: Path) -> Optional[Path]:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for directory in [current] + list(current.parents):
        candidate = directory / ".env"
        if candidate.exists():
            return candidate
    return None


def parse_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not _valid_env_key(key):
            continue
        values[key] = _strip_dotenv_value(value.strip())
    return values


def _valid_env_key(key: str) -> bool:
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(character.isalnum() or character == "_" for character in key)


def _strip_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        return value.split(" #", 1)[0].rstrip()
    return value
