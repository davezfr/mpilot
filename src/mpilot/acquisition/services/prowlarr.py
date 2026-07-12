from __future__ import annotations

import asyncio
import logging
import re

import httpx

from mpilot.acquisition.config import Settings
from mpilot.acquisition.domain.search_results import build_prowlarr_search_params, normalize_search_results
from mpilot.acquisition.exceptions import UpstreamServiceError
from mpilot.acquisition.domain.quality import extract_imdb_id, normalize_user_message
from mpilot.acquisition.models import IndexerImdbSearchMode, ProwlarrIndexer, SearchRequest, SearchResult


logger = logging.getLogger("qbitlarr-api.prowlarr")
PROWLARR_SEARCH_MAX_ATTEMPTS = 2
_BARE_IMDB_RE = re.compile(r"^(?:imdb:)?(tt\d{6,12})$", flags=re.IGNORECASE)


async def search_prowlarr(
    request: SearchRequest,
    settings: Settings,
) -> list[SearchResult]:
    imdb_id = _exact_imdb_id(request)
    if imdb_id and _imdb_routing_configured(settings):
        return await _search_imdb_by_indexer_mode(request, settings, imdb_id=imdb_id)
    return await _search_prowlarr_once(request, settings)


async def _search_imdb_by_indexer_mode(
    request: SearchRequest,
    settings: Settings,
    *,
    imdb_id: str,
) -> list[SearchResult]:
    native_ids = _scoped_indexer_ids(
        getattr(settings, "prowlarr_imdb_native_indexer_ids", None),
        request.indexer_ids,
    )
    keyword_ids = _scoped_indexer_ids(
        getattr(settings, "prowlarr_imdb_keyword_indexer_ids", None),
        request.indexer_ids,
    )
    searches = []
    if keyword_ids:
        searches.append(
            _search_prowlarr_once(
                request.model_copy(
                    update={
                        "identifier": None,
                        "query": imdb_id,
                        "indexer_ids": keyword_ids,
                    }
                ),
                settings,
                search_type="search",
            )
        )
    if native_ids:
        searches.append(
            _search_prowlarr_once(
                request.model_copy(
                    update={
                        "identifier": None,
                        "query": f"{{ImdbId:{imdb_id}}}",
                        "indexer_ids": native_ids,
                    }
                ),
                settings,
                search_type="movie",
            )
        )

    if not searches:
        logger.info("No configured IMDb-capable indexers are in scope for imdb_id=%s", imdb_id)
        return []

    groups = await asyncio.gather(*searches)
    merged = _merge_search_results(groups)
    logger.info(
        "IMDb-routed Prowlarr search returned %s usable results (keyword_ids=%s native_ids=%s)",
        len(merged),
        keyword_ids,
        native_ids,
    )
    return merged


async def _search_prowlarr_once(
    request: SearchRequest,
    settings: Settings,
    *,
    search_type: str = "search",
) -> list[SearchResult]:
    params = build_prowlarr_search_params(request, search_type=search_type)
    url = f"{settings.prowlarr_url}/api/v1/search"
    headers = {"X-Api-Key": settings.prowlarr_api_key}

    for attempt in range(1, PROWLARR_SEARCH_MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
                break
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            logger.warning("Prowlarr search failed with HTTP %s", status_code)
            raise UpstreamServiceError(f"Prowlarr search failed with HTTP {status_code}") from exc
        except httpx.TimeoutException as exc:
            if attempt < PROWLARR_SEARCH_MAX_ATTEMPTS:
                logger.warning("Prowlarr search timed out; retrying once")
                continue
            logger.warning("Prowlarr search request failed: %s", exc.__class__.__name__)
            raise UpstreamServiceError("Prowlarr is unreachable") from exc
        except httpx.RequestError as exc:
            logger.warning("Prowlarr search request failed: %s", exc.__class__.__name__)
            raise UpstreamServiceError("Prowlarr is unreachable") from exc
        except ValueError as exc:
            logger.warning("Prowlarr returned invalid JSON")
            raise UpstreamServiceError("Prowlarr returned invalid JSON") from exc

    if not isinstance(payload, list):
        raise UpstreamServiceError("Prowlarr returned an unexpected response shape")

    results = normalize_search_results(
        payload,
        prowlarr_url=settings.prowlarr_url,
        prowlarr_download_url=settings.prowlarr_download_url,
        prowlarr_api_key=settings.prowlarr_api_key,
    )
    logger.info("Prowlarr search returned %s usable results", len(results))
    return results


async def list_prowlarr_indexers(settings: Settings) -> list[ProwlarrIndexer]:
    url = f"{settings.prowlarr_url}/api/v1/indexer"
    payload = await _get_prowlarr_json(url, settings)
    if not isinstance(payload, list):
        raise UpstreamServiceError("Prowlarr returned an unexpected indexer response shape")

    indexers: list[ProwlarrIndexer] = []
    for item in payload:
        if not isinstance(item, dict) or "id" not in item:
            continue
        indexers.append(
            ProwlarrIndexer(
                id=int(item["id"]),
                name=_optional_str(item.get("name")),
                enabled=_optional_bool(item.get("enable", item.get("enabled"))),
                protocol=_optional_str(item.get("protocol")),
                supports_imdb_parameter=_supports_imdb_parameter(item),
                imdb_search_mode=_configured_imdb_search_mode(int(item["id"]), settings),
            )
        )
    return indexers


async def check_prowlarr_health(settings: Settings) -> dict[str, str]:
    url = f"{settings.prowlarr_url}/api/v1/system/status"
    try:
        await _get_prowlarr_json(url, settings)
    except UpstreamServiceError as exc:
        return {"status": "error", "detail": str(exc)}
    return {"status": "ok"}


async def _get_prowlarr_json(url: str, settings: Settings):
    headers = {"X-Api-Key": settings.prowlarr_api_key}
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning("Prowlarr request failed with HTTP %s", status_code)
        raise UpstreamServiceError(f"Prowlarr request failed with HTTP {status_code}") from exc
    except httpx.RequestError as exc:
        logger.warning("Prowlarr request failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("Prowlarr is unreachable") from exc
    except ValueError as exc:
        logger.warning("Prowlarr returned invalid JSON")
        raise UpstreamServiceError("Prowlarr returned invalid JSON") from exc


def _optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _exact_imdb_id(request: SearchRequest) -> str | None:
    if request.identifier and request.query:
        return None
    value = request.identifier or request.query
    if not value:
        return None
    normalized = normalize_user_message(value).strip()
    match = _BARE_IMDB_RE.fullmatch(normalized)
    if match:
        return match.group(1).lower()
    imdb_id = extract_imdb_id(normalized)
    if imdb_id and "imdb.com/title/" in normalized.casefold():
        return imdb_id
    return None


def _imdb_routing_configured(settings: Settings) -> bool:
    configured = getattr(settings, "imdb_indexer_routing_configured", None)
    if configured is not None:
        return bool(configured)
    return any(
        getattr(settings, name, None) is not None
        for name in (
            "prowlarr_imdb_native_indexer_ids",
            "prowlarr_imdb_keyword_indexer_ids",
            "prowlarr_imdb_disabled_indexer_ids",
        )
    )


def _scoped_indexer_ids(configured_ids: list[int] | None, requested_ids: list[int] | None) -> list[int]:
    configured = list(dict.fromkeys(configured_ids or []))
    if requested_ids is None:
        return configured
    requested = set(requested_ids)
    return [indexer_id for indexer_id in configured if indexer_id in requested]


def _merge_search_results(groups: list[list[SearchResult]]) -> list[SearchResult]:
    merged: list[SearchResult] = []
    seen: set[str] = set()
    for group in groups:
        for result in group:
            key = result.download_link.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(result)
    return merged


def _supports_imdb_parameter(item: dict) -> bool:
    capabilities = item.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    values = [
        *(capabilities.get("movieSearchParams") or []),
        *(capabilities.get("tvSearchParams") or []),
    ]
    return any(str(value).casefold() == "imdbid" for value in values)


def _configured_imdb_search_mode(indexer_id: int, settings: Settings) -> IndexerImdbSearchMode:
    if not _imdb_routing_configured(settings):
        return "legacy_keyword"
    if indexer_id in (getattr(settings, "prowlarr_imdb_native_indexer_ids", None) or []):
        return "native"
    if indexer_id in (getattr(settings, "prowlarr_imdb_keyword_indexer_ids", None) or []):
        return "keyword"
    if indexer_id in (getattr(settings, "prowlarr_imdb_disabled_indexer_ids", None) or []):
        return "disabled"
    return "unconfigured"
