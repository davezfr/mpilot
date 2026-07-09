from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException

from mpilot.acquisition.config import get_settings
from mpilot.acquisition.domain.download_progress import render_download_status_payload
from mpilot.acquisition.domain.quality import extract_imdb_id, infer_media_type
from mpilot.acquisition.domain.save_paths import default_save_path_for_title, validate_save_path_override
from mpilot.acquisition.domain.torrent_metadata import parse_torrent_name
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import DownloadRequest, DownloadResponse, SearchResult
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore
from mpilot.acquisition.services.qbittorrent import _download_torrent_file, add_download_to_qbittorrent


logger = logging.getLogger("qbitlarr-api.download")
router = APIRouter()


@router.post(
    "/download",
    response_model=DownloadResponse,
    operation_id="qbitlarr_download",
    summary="Queue a torrent or magnet in qBittorrent",
    tags=["qbitlarr"],
)
async def download(request: DownloadRequest) -> DownloadResponse:
    try:
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
    snapshot_input, snapshot_title, metadata = _snapshot_download_context(request, settings)
    title = snapshot_title
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
    if not request.query_id:
        return None, None, {}

    try:
        snapshot = QuerySnapshotStore(_query_snapshot_dir(settings)).read(request.query_id)
    except FileNotFoundError as exc:
        raise ValueError("query_id was not found") from exc

    request_input = _optional_string(snapshot.request.get("input")) if isinstance(snapshot.request, dict) else None
    search_query = _optional_string(snapshot.request.get("query")) if isinstance(snapshot.request, dict) else None
    metadata = _snapshot_metadata(request_input=request_input, search_query=search_query)
    selected_result = _find_snapshot_result_by_download_link(snapshot, request.download_link)
    if selected_result is None:
        return request_input, None, metadata
    return request_input, selected_result.title, metadata


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


async def _download_title_from_link(download_link: str, settings) -> str | None:
    parsed = urlparse(download_link)
    if parsed.scheme.lower() in {"http", "https"}:
        return parse_torrent_name(await _download_torrent_file(download_link, settings))

    if parsed.scheme.lower() == "magnet":
        names = parse_qs(parsed.query).get("dn") or []
        return names[0].strip() if names and names[0].strip() else None

    return None
