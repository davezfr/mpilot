from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


def env_first(*names: str, default: Optional[str] = None, env: Mapping[str, str] | None = None) -> Optional[str]:
    environment = env if env is not None else os.environ
    for name in names:
        value = environment.get(name)
        if value is not None:
            return value
    return default


def float_env(name: str, default: float, *, env: Mapping[str, str] | None = None) -> float:
    return float_env_any(name, default=default, env=env)


def int_env(name: str, default: int, *, env: Mapping[str, str] | None = None) -> int:
    return int_env_any(name, default=default, env=env)


def float_env_any(*names: str, default: float, env: Mapping[str, str] | None = None) -> float:
    raw_value = (env_first(*names, default="", env=env) or "").strip()
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def int_env_any(*names: str, default: int, env: Mapping[str, str] | None = None) -> int:
    raw_value = (env_first(*names, default="", env=env) or "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def read_env_file_value(path: Path, key: str) -> Optional[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    prefix = key + "="
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value.strip() or None
    return None


def hermes_env_paths(
    *explicit_env_names: str,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    environment = env if env is not None else os.environ
    explicit_path = (env_first(*explicit_env_names, default="", env=environment) or "").strip()
    if explicit_path:
        return [Path(explicit_path).expanduser()]

    paths: list[Path] = []
    hermes_home = environment.get("HERMES_HOME", "").strip()
    if hermes_home:
        paths.append(Path(hermes_home).expanduser() / ".env")
    paths.append((home if home is not None else Path.home()) / ".hermes" / ".env")

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            deduped.append(path)
            seen.add(key)
    return deduped


def telegram_bot_token(
    *token_env_names: str,
    hermes_env_path_names: tuple[str, ...],
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Optional[str]:
    environment = env if env is not None else os.environ
    for key in token_env_names:
        value = environment.get(key, "").strip()
        if value:
            return value

    for env_path in hermes_env_paths(*hermes_env_path_names, env=environment, home=home):
        value = read_env_file_value(env_path, "TELEGRAM_BOT_TOKEN")
        if value:
            return value
    return None
