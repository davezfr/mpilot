from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from mpilot.acquisition.config import get_settings
from mpilot.acquisition.exceptions import ConfigurationError
from mpilot.acquisition.models import QuerySnapshot
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore


logger = logging.getLogger("mpilot.acquisition.api.query-snapshots")
router = APIRouter()


@router.get(
    "/queries/{query_id}",
    response_model=QuerySnapshot,
    operation_id="acquisition_get_query_snapshot",
    summary="Get a saved MPilot acquisition query snapshot",
    description="Return the stored search snapshot document for a previous acquisition_handle query_id.",
    tags=["acquisition"],
)
async def get_query_snapshot(query_id: str) -> QuerySnapshot:
    try:
        settings = get_settings()
        return QuerySnapshotStore(settings.query_snapshot_dir).read(query_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Query snapshot not found") from exc
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
