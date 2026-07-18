from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Request

from mpilot.api.auth import bind_requester
from mpilot.acquisition.config import get_settings
from mpilot.acquisition.domain.download_progress import render_download_status_payload
from mpilot.acquisition.domain.imdb_identity import (
    is_downloadable_snapshot_result,
    validate_imdb_results,
    validate_title_year,
)
from mpilot.acquisition.domain.quality import extract_imdb_id, infer_media_type
from mpilot.acquisition.domain.save_paths import default_save_path_for_title, validate_save_path_override
from mpilot.acquisition.domain.torrent_metadata import parse_torrent_name
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import DownloadRequest, DownloadResponse, SearchResult
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore
from mpilot.acquisition.services.qbittorrent import _download_torrent_file, add_download_to_qbittorrent


logger = logging.getLogger("mpilot.acquisition.api.download")
router = APIRouter()


@dataclass(frozen=True)
class _SnapshotSelection:
    request_input: str | None
    search_query: str | None
    request_context: dict[str, Any]
    result: SearchResult


@router.post(
    "/download",
    response_model=DownloadResponse,
    operation_id="acquisition_download",
    summary="Queue a torrent or magnet in qBittorrent",
    tags=["acquisition"],
)
async def download(request: DownloadRequest, http_request: Request) -> DownloadResponse:
    try:
        effective_user_id = bind_requester(http_request, request.user_id)
        if effective_user_id != request.user_id:
            request = request.model_copy(update={"user_id": effective_user_id})
        settings = get_settings()
        save_path, metadata = await _resolve_download_context(request, settings)
        download_kwargs = {"save_path": save_path}
        if request.user_id:
            download_kwargs["requester_id"] = request.user_id
        download_status = await add_download_to_qbittorrent(
            request.download_link,
            settings,
            **download_kwargs,
        )
        rendered_status_payload = render_download_status_payload(download_status) if download_status else None
        return DownloadResponse(
            download_status=download_status,
            rendered_status=rendered_status_payload["message"] if rendered_status_payload else None,
            rendered_status_buttons=rendered_status_payload["buttons"] if rendered_status_payload else [],
            **metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _resolve_download_context(request: DownloadRequest, settings) -> tuple[str | None, dict[str, str]]:
    selection = _snapshot_selection(request, settings)
    snapshot_input = selection.request_input if selection else None
    snapshot_title = selection.result.title if selection else None
    metadata = _snapshot_metadata(
        request_input=snapshot_input,
        search_query=selection.search_query if selection else None,
    )
    title = await _verified_snapshot_title(request.download_link, selection, settings) if selection else None
    if not request.save_path and not title:
        title = await _download_title_from_link(request.download_link, settings)
    result_hints = [SearchResult(title=snapshot_title, download_link=request.download_link)] if snapshot_title else None
    media_type = infer_media_type(snapshot_input or title or "", result_hints)
    if snapshot_input or title or snapshot_title:
        metadata.setdefault("media_type", media_type)

    if request.save_path:
        return validate_save_path_override(request.save_path, settings), metadata

    return default_save_path_for_title(settings=settings, media_type=media_type, title=title or ""), metadata


async def _resolve_download_save_path(request: DownloadRequest, settings) -> str | None:
    save_path, _metadata = await _resolve_download_context(request, settings)
    return save_path


def _snapshot_download_context(request: DownloadRequest, settings) -> tuple[str | None, str | None, dict[str, str]]:
    selection = _snapshot_selection(request, settings)
    if selection is None:
        return None, None, {}

    metadata = _snapshot_metadata(
        request_input=selection.request_input,
        search_query=selection.search_query,
    )
    return selection.request_input, selection.result.title, metadata


def _snapshot_selection(request: DownloadRequest, settings) -> _SnapshotSelection | None:
    if not request.query_id:
        return None

    try:
        snapshot = QuerySnapshotStore(_query_snapshot_dir(settings)).read(request.query_id)
    except FileNotFoundError as exc:
        raise ValueError("query_id was not found") from exc

    snapshot_owner = snapshot.request.get("requester_id") if isinstance(snapshot.request, dict) else None
    if request.user_id and snapshot_owner != request.user_id:
        raise ValueError("query_id was not found")

    request_input = _optional_string(snapshot.request.get("input")) if isinstance(snapshot.request, dict) else None
    search_query = _optional_string(snapshot.request.get("query")) if isinstance(snapshot.request, dict) else None
    selected_result = _find_snapshot_result_by_download_link(snapshot, request.download_link)
    if selected_result is None:
        raise ValueError("download_link is not authorized for query_id")
    if not is_downloadable_snapshot_result(selected_result):
        raise ValueError("download_link is not verified for query_id")
    return _SnapshotSelection(
        request_input=request_input,
        search_query=search_query,
        request_context=snapshot.request,
        result=selected_result,
    )


async def _verified_snapshot_title(
    download_link: str,
    selection: _SnapshotSelection,
    settings,
) -> str:
    actual_title = await _download_title_from_link(download_link, settings)
    if not actual_title:
        return selection.result.title

    context = selection.request_context
    canonical_title = _optional_string(context.get("canonical_title"))
    title_aliases = _title_aliases(context.get("title_aliases"))
    canonical_year = _optional_year(context.get("canonical_year"))
    if selection.result.verification_status == "imdb_verified":
        imdb_id = _optional_string(context.get("imdb_id")) or _optional_string(context.get("query"))
        media_type = context.get("media_type")
        if not imdb_id or not canonical_title or media_type not in {"movie", "tv"}:
            raise ValueError("snapshot identity context is incomplete")
        validation = validate_imdb_results(
            [selection.result.model_copy(update={"title": actual_title})],
            imdb_id=imdb_id,
            canonical_title=canonical_title,
            title_aliases=title_aliases,
            year=canonical_year,
            media_type=media_type,
        )
        if not validation.verified_results:
            raise ValueError("download payload failed IMDb identity verification")
    elif (
        not canonical_title
        or canonical_year is None
        or validate_title_year(
            actual_title,
            canonical_title=canonical_title,
            title_aliases=title_aliases,
            year=canonical_year,
        )
        is None
    ):
        raise ValueError("download payload failed title/year verification")
    return selection.result.title


def _find_snapshot_result_by_download_link(snapshot, download_link: str):
    target = download_link.casefold()
    for entry in reversed(snapshot.snapshots):
        for result in entry.results:
            if result.download_link.casefold() == target:
                return result
    return None


def _query_snapshot_dir(settings) -> str:
    return getattr(settings, "query_snapshot_dir", "data/query-snapshots")


def _snapshot_metadata(*, request_input: str | None, search_query: str | None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    imdb_id = extract_imdb_id(request_input or "") or extract_imdb_id(search_query or "")
    if imdb_id:
        metadata["imdb_id"] = imdb_id
    return metadata


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_year(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1800 <= parsed <= 2200 else None


def _title_aliases(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


async def _download_title_from_link(download_link: str, settings) -> str | None:
    parsed = urlparse(download_link)
    if parsed.scheme.lower() in {"http", "https"}:
        return parse_torrent_name(await _download_torrent_file(download_link, settings))

    if parsed.scheme.lower() == "magnet":
        names = parse_qs(parsed.query).get("dn") or []
        return names[0].strip() if names and names[0].strip() else None

    return None
