from __future__ import annotations

import subprocess
from typing import Mapping

from .env import env_first, float_env_any


DEFAULT_HERMES_SEND_TIMEOUT_SECONDS = 20.0


def hermes_send_command(
    *,
    target: str,
    message: str,
    bin_name: str,
    profile: str = "",
) -> list[str]:
    command = [bin_name]
    if profile:
        command.extend(["--profile", profile])
    command.extend(["send", "--to", target, "--quiet", message])
    return command


def hermes_send_config(
    *,
    bin_env_names: tuple[str, ...],
    profile_env_names: tuple[str, ...],
    timeout_env_names: tuple[str, ...],
    env: Mapping[str, str] | None = None,
    default_bin: str = "hermes",
    default_timeout: float = DEFAULT_HERMES_SEND_TIMEOUT_SECONDS,
) -> tuple[str, str, float]:
    hermes_bin = env_first(*bin_env_names, default=default_bin, env=env) or default_bin
    hermes_profile = (env_first(*profile_env_names, default="", env=env) or "").strip()
    timeout_seconds = float_env_any(*timeout_env_names, default=default_timeout, env=env)
    return hermes_bin, hermes_profile, timeout_seconds


def decode_process_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def send_hermes_message(
    target: str,
    message: str,
    *,
    bin_env_names: tuple[str, ...],
    profile_env_names: tuple[str, ...],
    timeout_env_names: tuple[str, ...],
    env: Mapping[str, str] | None = None,
    default_timeout: float = DEFAULT_HERMES_SEND_TIMEOUT_SECONDS,
) -> None:
    hermes_bin, hermes_profile, timeout_seconds = hermes_send_config(
        bin_env_names=bin_env_names,
        profile_env_names=profile_env_names,
        timeout_env_names=timeout_env_names,
        env=env,
        default_timeout=default_timeout,
    )
    command = hermes_send_command(
        target=target,
        message=message,
        bin_name=hermes_bin,
        profile=hermes_profile,
    )
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("hermes send timed out after %g seconds" % timeout_seconds) from error
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        suffix = ": %s" % detail if detail else ""
        raise RuntimeError("hermes send failed with exit code %s%s" % (process.returncode, suffix))
