from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from mpilot.acquisition.config import get_settings
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import ProwlarrIndexer
from mpilot.acquisition.services.prowlarr import list_prowlarr_indexers


logger = logging.getLogger("qbitlarr-api.prowlarr-indexers")
router = APIRouter()


@router.get(
    "/prowlarr/indexers",
    response_model=list[ProwlarrIndexer],
    operation_id="qbitlarr_list_prowlarr_indexers",
    summary="List configured Prowlarr indexers",
    tags=["qbitlarr"],
)
async def prowlarr_indexers() -> list[ProwlarrIndexer]:
    try:
        settings = get_settings()
        return await list_prowlarr_indexers(settings)
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
