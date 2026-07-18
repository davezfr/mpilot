from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from mpilot.api.auth import require_snapshot_owner
from mpilot.api.handle import (
    DEFAULT_SEARCH_CATEGORIES,
    _choice_style,
    _manual_result_limit,
    _manual_results_response,
    _preferences,
)
from mpilot.acquisition.config import get_settings
from mpilot.acquisition.domain.complementary_search import validate_complementary_results
from mpilot.acquisition.domain.quality import contains_premium_quality_request, extract_requested_resolution
from mpilot.acquisition.exceptions import ConfigurationError, UpstreamServiceError
from mpilot.acquisition.models import HandleResponse, SearchRequest
from mpilot.acquisition.services.prowlarr import search_prowlarr
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore
from mpilot.acquisition.services.wikidata import resolve_imdb_metadata


logger = logging.getLogger("mpilot.acquisition.api.complementary-search")
router = APIRouter()

METADATA_UNAVAILABLE_MESSAGE = (
    "The IMDb ID search returned no identity-verified results, but reliable canonical title/year metadata "
    "was unavailable, so complementary search was not started."
)


@router.post(
    "/queries/{query_id}/complementary-search",
    response_model=HandleResponse,
    operation_id="acquisition_complementary_search",
    summary="Run an ownership-bound complementary title/year search",
    description=(
        "Resolve canonical metadata from the saved IMDb query and search only configured complementary indexers. "
        "The operation accepts no caller-provided search text and never auto-downloads."
    ),
    tags=["acquisition"],
)
async def complementary_search(query_id: str, request: Request) -> HandleResponse:
    try:
        settings = get_settings()
        store = QuerySnapshotStore(settings.query_snapshot_dir)
        snapshot = store.read(query_id)
        owner_id = snapshot.request.get("requester_id") if isinstance(snapshot.request, dict) else None
        require_snapshot_owner(request, owner_id if isinstance(owner_id, str) else None)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Query snapshot not found") from exc
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    request_context = snapshot.request if isinstance(snapshot.request, dict) else {}
    imdb_id = _optional_string(request_context.get("imdb_id")) or _optional_string(request_context.get("query"))
    media_type = _media_type(request_context.get("media_type"))
    categories = request_context.get("categories")
    if not isinstance(categories, list) or not all(isinstance(value, int) for value in categories):
        categories = list(DEFAULT_SEARCH_CATEGORIES)
    trigger = "automatic_empty" if snapshot.status == "imdb_empty" else "user_requested"

    try:
        metadata = await resolve_imdb_metadata(imdb_id or "", settings)
    except UpstreamServiceError as exc:
        store.append(
            query_id=query_id,
            status="complementary_error",
            reason="complementary_metadata_error",
            results=[],
            metadata={"trigger": trigger, "metadata_source": "wikidata"},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not metadata:
        store.append(
            query_id=query_id,
            status="complementary_metadata_unavailable",
            reason="complementary_metadata_unavailable",
            results=[],
            metadata={"trigger": trigger, "metadata_source": "wikidata"},
        )
        return HandleResponse(
            status="not_found",
            action="show_results",
            message=METADATA_UNAVAILABLE_MESSAGE,
            message_key="complementary_metadata_unavailable",
            query_id=query_id,
            snapshot_status="complementary_metadata_unavailable",
            imdb_id=imdb_id,
            media_type=media_type,
            search_strategy="complementary",
            results_verified_by_imdb_id=False,
            results=[],
        )

    canonical_title = str(metadata["canonical_title"])
    year = int(metadata["year"])
    query_used = f"{canonical_title} {year}"
    media_type = _media_type(metadata.get("media_type")) or media_type
    indexer_ids = list(dict.fromkeys(settings.prowlarr_complementary_indexer_ids or []))
    original_input = _optional_string(request_context.get("input")) or ""
    requested_resolution = extract_requested_resolution(original_input)
    entry_metadata = {
        "trigger": trigger,
        "query_used": query_used,
        "metadata_source": metadata.get("metadata_source", "wikidata"),
        "indexer_ids": indexer_ids,
        "search_strategy": "complementary",
        "results_verified_by_imdb_id": False,
    }

    if not indexer_ids:
        store.append(
            query_id=query_id,
            status="complementary_empty",
            reason="complementary_not_configured",
            results=[],
            metadata=entry_metadata,
        )
        return _empty_response(
            query_id=query_id,
            imdb_id=imdb_id,
            media_type=media_type,
            query_used=query_used,
            snapshot_status="complementary_empty",
        )

    try:
        raw_results = await search_prowlarr(
            SearchRequest(
                query=query_used,
                categories=categories,
                indexer_ids=indexer_ids,
                result_resolution=requested_resolution or _preferences(settings).resolution,
            ),
            settings,
        )
    except UpstreamServiceError as exc:
        store.append(
            query_id=query_id,
            status="complementary_error",
            reason="complementary_upstream_error",
            results=[],
            metadata=entry_metadata,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    title_aliases = metadata.get("title_aliases") if isinstance(metadata.get("title_aliases"), list) else []
    validated = validate_complementary_results(
        raw_results,
        canonical_title=canonical_title,
        title_aliases=title_aliases,
        year=year,
    )
    snapshot_status = "complementary_ready" if validated else "complementary_empty"
    store.append(
        query_id=query_id,
        status=snapshot_status,
        reason="complementary_results_ready" if validated else "complementary_no_valid_results",
        results=validated,
        metadata={**entry_metadata, "raw_result_count": len(raw_results), "validated_result_count": len(validated)},
    )
    if not validated:
        return _empty_response(
            query_id=query_id,
            imdb_id=imdb_id,
            media_type=media_type,
            query_used=query_used,
            snapshot_status=snapshot_status,
        )

    return _manual_results_response(
        sorted(validated, key=lambda result: (result.seeders or 0, result.size or 0), reverse=True),
        status="success",
        message=_results_message(trigger=trigger, query_used=query_used),
        message_key=(
            "complementary_results_automatic"
            if trigger == "automatic_empty"
            else "complementary_results_user_requested"
        ),
        message_params={"query_used": query_used},
        media_type=media_type or "movie",
        prefer_premium=contains_premium_quality_request(original_input),
        requested_resolution=requested_resolution,
        query_id=query_id,
        snapshot_status=snapshot_status,
        preferences=_preferences(settings),
        compact_labels=False,
        manual_result_limit=_manual_result_limit(settings),
        choice_style=_choice_style(settings),
        imdb_id=imdb_id,
        search_strategy="complementary",
        query_used=query_used,
        results_verified_by_imdb_id=False,
    )


def _results_message(*, trigger: str, query_used: str) -> str:
    if trigger == "automatic_empty":
        return (
            "The IMDb ID search returned no identity-verified results. "
            f'A complementary search was run using the canonical title and year "{query_used}". '
            "These results are not verified by IMDb ID; review them before choosing."
        )
    return (
        f'A complementary search was requested using the canonical title and year "{query_used}". '
        "Results may repeat earlier choices and are not verified by IMDb ID; review them before choosing."
    )


def _empty_response(
    *,
    query_id: str,
    imdb_id: str | None,
    media_type: str | None,
    query_used: str,
    snapshot_status: str,
) -> HandleResponse:
    return HandleResponse(
        status="not_found",
        action="show_results",
        message=f'No complementary results matched the canonical title and year "{query_used}".',
        message_key="complementary_no_results",
        message_params={"query_used": query_used},
        query_id=query_id,
        snapshot_status=snapshot_status,
        imdb_id=imdb_id,
        media_type=media_type,
        search_strategy="complementary",
        query_used=query_used,
        results_verified_by_imdb_id=False,
        results=[],
    )


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _media_type(value: object) -> str | None:
    return value if value in {"movie", "tv"} else None
