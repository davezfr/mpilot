from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from mpilot.acquisition.config import get_settings
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import SearchRequest, SearchResult
from mpilot.acquisition.services.prowlarr import search_prowlarr


logger = logging.getLogger("mpilot.acquisition.api.search")
router = APIRouter()


@router.post(
    "/search",
    response_model=list[SearchResult],
    operation_id="acquisition_search",
    summary="Search Prowlarr",
    tags=["acquisition"],
)
async def search(request: SearchRequest) -> list[SearchResult]:
    try:
        settings = get_settings()
        return await search_prowlarr(request, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except UpstreamServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
