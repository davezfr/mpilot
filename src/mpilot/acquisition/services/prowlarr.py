from __future__ import annotations

import logging

import httpx

from mpilot.acquisition.config import Settings
from mpilot.acquisition.domain.search_results import build_prowlarr_search_params, normalize_search_results
from mpilot.acquisition.exceptions import UpstreamServiceError
from mpilot.acquisition.models import ProwlarrIndexer, SearchRequest, SearchResult


logger = logging.getLogger("qbitlarr-api.prowlarr")
PROWLARR_SEARCH_MAX_ATTEMPTS = 2


async def search_prowlarr(
    request: SearchRequest,
    settings: Settings,
) -> list[SearchResult]:
    params = build_prowlarr_search_params(request)
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
