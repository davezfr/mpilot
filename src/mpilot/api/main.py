from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from mpilot.core.dotenv import load_project_dotenv

from fastapi import FastAPI, Request
from fastapi_mcp import FastApiMCP
from starlette.responses import JSONResponse

from mpilot.api.download import router as download_router
from mpilot.api.complementary_search import router as complementary_search_router
from mpilot.api.downloads_list import router as downloads_list_router
from mpilot.api.handle import get_categories, router as handle_router
from mpilot.api.prowlarr import router as prowlarr_router
from mpilot.api.query_snapshots import router as query_snapshots_router
from mpilot.api.search import router as search_router
from mpilot.api.auth import ApiAuthConfigurationError, authenticate_api_key, requester_api_keys
from mpilot.acquisition.config import Settings, get_settings
from mpilot.acquisition.domain.quality import calculate_score
from mpilot.acquisition.domain.search_results import build_prowlarr_search_params, normalize_search_results
from mpilot.acquisition.env import env_first as acquisition_env_first
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import (
    DownloadRequest,
    DownloadResponse,
    DynamicProgressWatchPolicy,
    HandleRequest,
    HandleResponse,
    ManualSearchResult,
    ProwlarrIndexer,
    QuerySnapshot,
    QuerySnapshotEntry,
    RenderedDownloadStatusResponse,
    RenderedDownloadsStatusResponse,
    SearchRequest,
    SearchResult,
    TorrentStatus,
    normalize_download_link,
)
from mpilot.acquisition.services.prowlarr import check_prowlarr_health, list_prowlarr_indexers, search_prowlarr
from mpilot.acquisition.services.qbittorrent import (
    add_download_to_qbittorrent,
    check_qbittorrent_health,
    cleanup_completed_downloads_from_qbittorrent,
    get_download_status_from_qbittorrent,
    list_downloads_from_qbittorrent,
)
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore

load_project_dotenv()


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("mpilot.acquisition.api")

_cleanup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await start_cleanup_task()
    try:
        yield
    finally:
        await stop_cleanup_task()


app = FastAPI(title="MPilot Acquisition API", lifespan=lifespan)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    try:
        configured_requester_keys = requester_api_keys()
        has_configured_key = bool(
            (acquisition_env_first("QBITLARR_API_KEY", default="") or "").strip()
            or configured_requester_keys
        )
        authenticated, requester_id = authenticate_api_key(request.headers.get("X-API-Key", ""))
    except ApiAuthConfigurationError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    if authenticated:
        request.state.auth_is_admin = requester_id is None
        request.state.auth_requester_id = requester_id
    elif not has_configured_key and _env_truthy("MPILOT_ALLOW_UNAUTHENTICATED_LOOPBACK") and _is_loopback_request(request):
        request.state.auth_is_admin = True
        request.state.auth_requester_id = None
    elif has_configured_key:
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
    else:
        return JSONResponse(status_code=401, content={"detail": "MPilot acquisition API key is required"})
    return await call_next(request)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _is_loopback_request(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    if host in {"testclient", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def start_cleanup_task() -> None:
    global _cleanup_task
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        logger.warning("Cleanup task not started because configuration is incomplete: %s", exc)
        return

    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_completed_downloads_loop(settings))


async def stop_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is None:
        return
    _cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await _cleanup_task
    _cleanup_task = None


async def _cleanup_completed_downloads_loop(
    settings: Settings,
    *,
    cleanup_func: Callable[[Settings], Awaitable[dict]] = cleanup_completed_downloads_from_qbittorrent,
    snapshot_prune_func: Callable[[Settings], Awaitable[dict]] | None = None,
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if snapshot_prune_func is None:
        snapshot_prune_func = _prune_query_snapshots
    interval = max(float(getattr(settings, "cleanup_interval_seconds", 21_600)), 60.0)
    while True:
        if getattr(settings, "cleanup_enabled", True):
            try:
                summary = await cleanup_func(settings)
                deleted_count = int(summary.get("deleted_count", 0))
                if deleted_count:
                    logger.info("Cleaned up %s completed MPilot acquisition torrent task(s)", deleted_count)
            except asyncio.CancelledError:
                raise
            except UpstreamServiceError as exc:
                logger.warning("Completed download cleanup failed: %s", exc)
            except Exception:
                logger.exception("Completed download cleanup failed unexpectedly")
        try:
            snapshot_summary = await snapshot_prune_func(settings)
            snapshot_deleted_count = int(snapshot_summary.get("deleted_count", 0))
            if snapshot_deleted_count:
                logger.info("Pruned %s MPilot acquisition query snapshot(s)", snapshot_deleted_count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Query snapshot prune failed unexpectedly")
        await sleep_func(interval)


async def _prune_query_snapshots(settings: Settings) -> dict:
    retention_seconds = int(getattr(settings, "query_snapshot_retention_seconds", 604_800))
    return await asyncio.to_thread(
        QuerySnapshotStore(getattr(settings, "query_snapshot_dir", "data/query-snapshots")).prune,
        now=datetime.now(UTC),
        retention=timedelta(seconds=max(retention_seconds, 0)),
    )


app.include_router(search_router)
app.include_router(download_router)
app.include_router(downloads_list_router)
app.include_router(handle_router)
app.include_router(prowlarr_router)
app.include_router(query_snapshots_router)
app.include_router(complementary_search_router)


@app.get(
    "/health",
    operation_id="acquisition_health",
    summary="Check MPilot acquisition API health",
    tags=["acquisition"],
)
async def health(deep: bool = False):
    if not deep:
        return {"status": "ok", "service": "MPilot Acquisition API"}

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "service": "MPilot Acquisition API",
                "dependencies": {
                    "config": {"status": "error", "detail": str(exc)},
                },
            },
        )

    prowlarr_status = await check_prowlarr_health(settings)
    qbittorrent_status = await check_qbittorrent_health(settings)
    dependencies = {
        "prowlarr": prowlarr_status,
        "qbittorrent": qbittorrent_status,
    }
    status = "ok" if all(item.get("status") == "ok" for item in dependencies.values()) else "degraded"
    payload = {
        "status": status,
        "service": "MPilot Acquisition API",
        "dependencies": dependencies,
    }
    if status != "ok":
        return JSONResponse(status_code=503, content=payload)
    return payload


ACQUISITION_MCP_OPERATIONS = [
    "acquisition_complementary_search",
    "acquisition_delete_download",
    "acquisition_download",
    "acquisition_get_download_status",
    "acquisition_get_query_snapshot",
    "acquisition_handle",
    "acquisition_health",
    "acquisition_list_downloads",
    "acquisition_list_indexers",
    "acquisition_pause_download",
    "acquisition_render_download_status",
    "acquisition_render_downloads_status",
    "acquisition_resume_download",
    "acquisition_search",
]


mcp = FastApiMCP(
    app,
    name="MPilot Acquisition",
    description="Safely search movie and TV requests and add selected downloads to qBittorrent.",
    include_operations=ACQUISITION_MCP_OPERATIONS,
)
mcp.mount_http(mount_path="/mcp")


__all__ = [
    "ConfigurationError",
    "DownloadRequest",
    "DownloadResponse",
    "DynamicProgressWatchPolicy",
    "HandleRequest",
    "HandleResponse",
    "ManualSearchResult",
    "ProwlarrIndexer",
    "QuerySnapshot",
    "QuerySnapshotEntry",
    "RenderedDownloadStatusResponse",
    "RenderedDownloadsStatusResponse",
    "SearchRequest",
    "SearchResult",
    "Settings",
    "TorrentStatus",
    "UpstreamServiceError",
    "add_download_to_qbittorrent",
    "app",
    "build_prowlarr_search_params",
    "calculate_score",
    "get_categories",
    "get_settings",
    "get_download_status_from_qbittorrent",
    "health",
    "list_prowlarr_indexers",
    "list_downloads_from_qbittorrent",
    "mcp",
    "normalize_download_link",
    "normalize_search_results",
    "search_prowlarr",
]
