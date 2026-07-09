from __future__ import annotations

from typing import Any, Mapping


PROGRESS_BAR_WIDTH = 10
DYNAMIC_PROGRESS_MAX_DURATION_SECONDS = 15 * 60
DYNAMIC_PROGRESS_UPDATE_INTERVAL_SECONDS = 3
DYNAMIC_PROGRESS_MIN_DELTA = 0.03
TRACKING_EXPIRED_MESSAGE = "Still downloading. Ask for status again to refresh; completion will still notify you."

COMPLETE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "forcedUP", "queuedUP"}
# qBittorrent v5 renamed pausedDL/pausedUP to stoppedDL/stoppedUP. Match both
# so the title icon and bar color stay accurate across upgrades.
PAUSED_STATES = {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
ERROR_STATES = {"error", "missingFiles", "unknown"}

PROGRESS_CELL_EMPTY = "⬜"
PROGRESS_CELL_DOWNLOADING = "🟩"
PROGRESS_CELL_PAUSED = "🟧"
PROGRESS_CELL_ERROR = "🟥"
PROGRESS_CELL_COMPLETE = "✅"


def render_download_status(status: Any) -> str:
    payload = _status_payload(status)
    name = _string_value(payload.get("name")) or _string_value(payload.get("hash")) or "Download"
    progress = _progress_value(payload.get("progress"))
    size = _optional_int(payload.get("size"))

    lines = [
        f"{_status_icon(payload, progress)} {name}",
        f"{_progress_bar(progress, payload=payload)} {_format_percent(progress, payload=payload)}",
    ]

    size_line = _size_line(size=size, progress=progress)
    if size_line:
        lines.append(size_line)

    speed = _optional_int(payload.get("download_speed"))
    speed_line = _rate_line(speed)
    if speed_line:
        lines.append(speed_line)

    eta = _optional_int(payload.get("eta"))
    eta_line = _eta_line(eta)
    if eta_line:
        lines.append(eta_line)

    return "\n".join(lines)


def render_download_status_payload(status: Any) -> dict[str, Any]:
    payload = _status_payload(status)
    return {
        "message": render_download_status(status),
        "buttons": _control_buttons(payload),
    }


def render_downloads_status(statuses: list[Any]) -> str:
    if not statuses:
        return "No active downloads."
    return "\n\n".join(render_download_status(status) for status in statuses)


def render_tracking_expired_status(status: Any, *, timeout_message: str | None = None) -> str:
    message = render_download_status(status)
    if _is_complete(_status_payload(status)):
        return message
    return f"{message}\n{timeout_message or TRACKING_EXPIRED_MESSAGE}"


def dynamic_progress_watch_policy() -> dict[str, Any]:
    return {
        "mode": "bounded_edit_loop",
        "max_duration_seconds": DYNAMIC_PROGRESS_MAX_DURATION_SECONDS,
        "update_interval_seconds": DYNAMIC_PROGRESS_UPDATE_INTERVAL_SECONDS,
        "min_progress_delta": DYNAMIC_PROGRESS_MIN_DELTA,
        "completion_notifications_are_separate": True,
        "timeout_message": TRACKING_EXPIRED_MESSAGE,
    }


def _control_buttons(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    info_hash = _string_value(payload.get("hash"))
    if not info_hash or _is_complete(payload):
        return []

    state = str(payload.get("state") or "")
    if state in PAUSED_STATES:
        primary = {"text": "▶️", "callback_data": f"dl:{info_hash}:resume"}
    else:
        primary = {"text": "⏸️", "callback_data": f"dl:{info_hash}:pause"}
    return [
        primary,
        {"text": "❌", "callback_data": f"dl:{info_hash}:delete"},
    ]


def _status_payload(status: Any) -> Mapping[str, Any]:
    if isinstance(status, Mapping):
        return status
    if hasattr(status, "model_dump"):
        return status.model_dump()
    return {
        "name": getattr(status, "name", None),
        "state": getattr(status, "state", None),
        "progress": getattr(status, "progress", None),
        "size": getattr(status, "size", None),
        "download_speed": getattr(status, "download_speed", None),
        "eta": getattr(status, "eta", None),
        "hash": getattr(status, "hash", None),
    }


def _status_icon(payload: Mapping[str, Any], progress: float) -> str:
    state = str(payload.get("state") or "")
    if progress >= 1.0 or state in COMPLETE_STATES:
        return "✅"
    if state in PAUSED_STATES:
        return "⏸️"
    if state in ERROR_STATES:
        return "⚠️"
    return "⬇️"


def _progress_bar(
    progress: float,
    *,
    width: int = PROGRESS_BAR_WIDTH,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Render a 10-cell emoji progress bar.

    Complete shows a single ✅ so 99% and 100% are unambiguous instead of
    differing by one cell. Paused and errored runs tint the filled cells
    (🟧 / 🟥) so the bar color matches the title state icon at a glance.
    """
    clamped = _clamp(progress)
    if clamped >= 1.0 or _state_is_complete(payload):
        return PROGRESS_CELL_COMPLETE
    state = str((payload or {}).get("state") or "")
    if state in ERROR_STATES:
        filled_cell = PROGRESS_CELL_ERROR
    elif state in PAUSED_STATES:
        filled_cell = PROGRESS_CELL_PAUSED
    else:
        filled_cell = PROGRESS_CELL_DOWNLOADING
    filled = int(clamped * width)
    return (filled_cell * filled) + (PROGRESS_CELL_EMPTY * (width - filled))


def _state_is_complete(payload: Mapping[str, Any] | None) -> bool:
    if payload is None:
        return False
    return str(payload.get("state") or "") in COMPLETE_STATES


def _format_percent(progress: float, *, payload: Mapping[str, Any] | None = None) -> str:
    clamped = _clamp(progress)
    if clamped >= 1.0 or _state_is_complete(payload):
        return "100%"
    return f"{min(int(clamped * 100), 99)}%"


def _size_line(*, size: int | None, progress: float) -> str | None:
    if size is None or size <= 0:
        return None
    downloaded = int(size * _clamp(progress))
    return f"💾 {_format_size(downloaded)} / {_format_size(size)}"


def _rate_line(speed: int | None) -> str | None:
    if speed is None or speed <= 0:
        return None
    return f"⚡ Speed: {_format_size(speed)}/s"


def _eta_line(eta: int | None) -> str | None:
    if eta is None or eta <= 0 or eta >= 8_640_000:
        return None
    return f"⏱️ ETA: {_format_duration(eta)}"


def _format_duration(seconds: int) -> str:
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)

    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    return f"{seconds}s"


def _format_size(value: int) -> str:
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    unit_index = 0
    while abs(size) >= 1000 and unit_index < len(units) - 1:
        size /= 1000
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} B"
    formatted = f"{size:.1f}".rstrip("0").rstrip(".")
    return f"{formatted} {units[unit_index]}"


def _is_complete(payload: Mapping[str, Any]) -> bool:
    return _progress_value(payload.get("progress")) >= 1.0 or str(payload.get("state") or "") in COMPLETE_STATES


def _progress_value(value: Any) -> float:
    try:
        return _clamp(float(value))
    except (TypeError, ValueError):
        return 0.0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clamp(value: float) -> float:
    return max(0.0, min(value, 1.0))
