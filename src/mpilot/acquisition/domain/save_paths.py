from __future__ import annotations

import posixpath
import re

from mpilot.acquisition.config import Settings
from mpilot.acquisition.domain.quality import MediaType, clean_display_title, parse_quality


DEFAULT_ALLOWED_SAVE_PATHS = (
    "/downloads/movies",
    "/downloads/movies-4k",
    "/downloads/tv",
)


def validate_save_path_override(save_path: str | None, settings: Settings) -> str | None:
    if save_path is None:
        return None

    normalized = _canonical_posix_path(save_path)
    allowed_roots = [_canonical_posix_path(path) for path in _allowed_save_paths(settings) if path]
    if any(_is_same_or_child(normalized, root) for root in allowed_roots):
        return normalized

    raise ValueError("save_path must be inside a configured qBitlarr save path")


def default_save_path_for_title(
    *,
    settings: Settings,
    media_type: MediaType,
    title: str,
) -> str:
    if media_type == "tv":
        return _join_path(
            getattr(settings, "qbitlarr_save_path_tv", DEFAULT_ALLOWED_SAVE_PATHS[2]),
            _tv_show_folder_name(title),
        )
    if parse_quality(title).resolution == "2160p":
        return getattr(settings, "qbitlarr_save_path_movie_4k", DEFAULT_ALLOWED_SAVE_PATHS[1])
    return getattr(settings, "qbitlarr_save_path_movie", DEFAULT_ALLOWED_SAVE_PATHS[0])


def _allowed_save_paths(settings: Settings) -> list[str]:
    configured = [
        getattr(settings, "qbitlarr_save_path_movie", DEFAULT_ALLOWED_SAVE_PATHS[0]),
        getattr(settings, "qbitlarr_save_path_movie_4k", DEFAULT_ALLOWED_SAVE_PATHS[1]),
        getattr(settings, "qbitlarr_save_path_tv", DEFAULT_ALLOWED_SAVE_PATHS[2]),
    ]
    configured.extend(getattr(settings, "qbitlarr_extra_save_paths", None) or [])
    return configured


def _normalize_path(path: str) -> str:
    return _canonical_posix_path(path)


def _is_same_or_child(path: str, root: str) -> bool:
    if path == root:
        return True
    return posixpath.commonpath([path, root]) == root


def _canonical_posix_path(path: str) -> str:
    value = path.strip()
    if not value.startswith("/") or "\\" in value or "\0" in value:
        raise ValueError("save_path must be inside a configured qBitlarr save path")
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("save_path must be inside a configured qBitlarr save path")
    normalized = "/" + "/".join(parts)
    if normalized == "/":
        raise ValueError("save_path must be inside a configured qBitlarr save path")
    return normalized


_TV_SHOW_MARKER_RE = re.compile(
    r"\b(S\d{1,2}(?:E\d{1,3})?|Season\s+\d{1,2}|Complete\s+Season)\b.*$",
    re.IGNORECASE,
)


def _tv_show_folder_name(title: str) -> str | None:
    display_title = clean_display_title(title)
    marker = _TV_SHOW_MARKER_RE.search(display_title)
    if marker:
        display_title = display_title[: marker.start()]

    folder_name = _sanitize_folder_name(display_title)
    return folder_name or None


def _sanitize_folder_name(name: str) -> str:
    sanitized = name.replace("/", " ").replace("\\", " ")
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized.strip(" ._-")


def _join_path(base_path: str, child_name: str | None) -> str:
    base = _normalize_path(base_path)
    child = _sanitize_folder_name(child_name or "")
    if not child:
        return base
    if base == "/":
        return f"/{child}"
    return f"{base}/{child}"
