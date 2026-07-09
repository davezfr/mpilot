from __future__ import annotations

from typing import Optional


def string_or_none(value: object) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def looks_like_hermes_target(value: Optional[str]) -> bool:
    if not value:
        return False
    platform, separator, target_ref = value.partition(":")
    if not separator or not target_ref.strip():
        return False
    return all(char.isalnum() or char in {"_", "-"} for char in platform)


def resolve_notification_target(notification_target: Optional[str], requester_id: Optional[str]) -> Optional[str]:
    explicit_target = string_or_none(notification_target)
    if explicit_target:
        return explicit_target
    requester_target = string_or_none(requester_id)
    if looks_like_hermes_target(requester_target):
        return requester_target
    return None


def parse_telegram_target(target: str) -> Optional[tuple[str, Optional[str]]]:
    platform, separator, target_ref = str(target or "").partition(":")
    if platform.casefold() != "telegram" or not separator:
        return None
    parts = [part.strip() for part in target_ref.split(":") if part.strip()]
    if not parts:
        return None
    chat_id = parts[0]
    thread_id = parts[1] if len(parts) > 1 else None
    return chat_id, thread_id
