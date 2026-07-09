from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from mpilot.acquisition.config import get_settings
from mpilot.acquisition.domain.download_progress import (
    dynamic_progress_watch_policy,
    render_download_status_payload,
    render_download_status,
    render_downloads_status,
)
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import (
    DownloadControlResponse,
    DynamicProgressWatchPolicy,
    RenderedDownloadStatusResponse,
    RenderedDownloadsStatusResponse,
    TorrentStatus,
    normalize_optional_user_id,
)
from mpilot.acquisition.services.qbittorrent import (
    delete_download_from_qbittorrent,
    get_download_status_from_qbittorrent,
    list_downloads_from_qbittorrent,
    pause_download_in_qbittorrent,
    resume_download_in_qbittorrent,
)


logger = logging.getLogger("mpilot.acquisition.api.downloads")
router = APIRouter()


@router.get(
    "/downloads",
    response_model=list[TorrentStatus],
    operation_id="acquisition_list_downloads",
    summary="List qBittorrent downloads",
    tags=["acquisition"],
)
async def list_downloads(
    user_id: str | None = Query(
        default=None,
        description="Optional requester identifier. When set, only torrents tagged for that requester are returned.",
    ),
) -> list[TorrentStatus]:
    try:
        settings = get_settings()
        return await list_downloads_from_qbittorrent(settings, requester_id=normalize_optional_user_id(user_id))
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/downloads/status-message",
    response_model=RenderedDownloadsStatusResponse,
    operation_id="acquisition_render_downloads_status",
    summary="Render qBittorrent downloads as a chat progress message",
    tags=["acquisition"],
)
async def render_downloads_status_message(
    user_id: str | None = Query(
        default=None,
        description="Optional requester identifier. When set, only torrents tagged for that requester are returned.",
    ),
) -> RenderedDownloadsStatusResponse:
    try:
        settings = get_settings()
        downloads = await list_downloads_from_qbittorrent(
            settings,
            requester_id=normalize_optional_user_id(user_id),
        )
        return RenderedDownloadsStatusResponse(
            message=render_downloads_status(downloads),
            watch_policy=_watch_policy(),
            downloads=downloads,
        )
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/downloads/{info_hash}/status-message",
    response_model=RenderedDownloadStatusResponse,
    operation_id="acquisition_render_download_status",
    summary="Render one qBittorrent download as a chat progress message",
    tags=["acquisition"],
)
async def render_download_status_message(
    info_hash: str,
    user_id: str | None = Query(
        default=None,
        description="Optional requester identifier. When set, the torrent must be tagged for that requester.",
    ),
) -> RenderedDownloadStatusResponse:
    try:
        settings = get_settings()
        status = await get_download_status_from_qbittorrent(
            settings,
            info_hash,
            requester_id=normalize_optional_user_id(user_id),
        )
        if status is None:
            raise HTTPException(status_code=404, detail="Download not found")
        rendered = render_download_status_payload(status)
        return RenderedDownloadStatusResponse(
            message=rendered["message"],
            watch_policy=_watch_policy(),
            download=status,
            buttons=rendered["buttons"],
        )
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/downloads/{info_hash}",
    response_model=TorrentStatus,
    operation_id="acquisition_get_download_status",
    summary="Get one qBittorrent download by info hash",
    tags=["acquisition"],
)
async def get_download_status(
    info_hash: str,
    user_id: str | None = Query(
        default=None,
        description="Optional requester identifier. When set, the torrent must be tagged for that requester.",
    ),
) -> TorrentStatus:
    try:
        settings = get_settings()
        status = await get_download_status_from_qbittorrent(
            settings,
            info_hash,
            requester_id=normalize_optional_user_id(user_id),
        )
        if status is None:
            raise HTTPException(status_code=404, detail="Download not found")
        return status
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/downloads/{info_hash}/pause",
    response_model=DownloadControlResponse,
    operation_id="acquisition_pause_download",
    summary="Pause one qBittorrent download by info hash",
    tags=["acquisition"],
)
async def pause_download(
    info_hash: str,
    user_id: str = Query(
        ...,
        description="Required requester identifier. The torrent must be tagged for this requester.",
    ),
) -> DownloadControlResponse:
    return await _control_download(info_hash, user_id=user_id, action="pause")


@router.post(
    "/downloads/{info_hash}/resume",
    response_model=DownloadControlResponse,
    operation_id="acquisition_resume_download",
    summary="Resume one qBittorrent download by info hash",
    tags=["acquisition"],
)
async def resume_download(
    info_hash: str,
    user_id: str = Query(
        ...,
        description="Required requester identifier. The torrent must be tagged for this requester.",
    ),
) -> DownloadControlResponse:
    return await _control_download(info_hash, user_id=user_id, action="resume")


@router.post(
    "/downloads/{info_hash}/delete",
    response_model=DownloadControlResponse,
    operation_id="acquisition_delete_download",
    summary="Delete one qBittorrent download task by info hash",
    tags=["acquisition"],
)
async def delete_download(
    info_hash: str,
    user_id: str = Query(
        ...,
        description="Required requester identifier. The torrent must be tagged for this requester.",
    ),
) -> DownloadControlResponse:
    return await _control_download(info_hash, user_id=user_id, action="delete")


async def _control_download(info_hash: str, *, user_id: str, action: str) -> DownloadControlResponse:
    requester_id = normalize_optional_user_id(user_id)
    if not requester_id:
        raise HTTPException(status_code=422, detail="user_id is required")

    try:
        settings = get_settings()
        if action == "pause":
            status = await pause_download_in_qbittorrent(settings, info_hash, requester_id=requester_id)
        elif action == "resume":
            status = await resume_download_in_qbittorrent(settings, info_hash, requester_id=requester_id)
        elif action == "delete":
            status = await delete_download_from_qbittorrent(settings, info_hash, requester_id=requester_id)
        else:
            raise HTTPException(status_code=400, detail="Unsupported download control action")
        if status is None:
            raise HTTPException(status_code=404, detail="Download not found")
        return DownloadControlResponse(action=action, download=status)
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _watch_policy() -> DynamicProgressWatchPolicy:
    return DynamicProgressWatchPolicy(**dynamic_progress_watch_policy())
