from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from mpilot.acquisition.domain.quality import extract_imdb_id, normalize_user_message
from mpilot.acquisition.models import SearchRequest, SearchResult, normalize_download_link


MAX_SEARCH_RESULTS = 20
PROWLARR_UPSTREAM_LIMIT = 50

IDENTIFIER_PARAM_NAMES = {
    "imdb": "ImdbId",
    "imdbid": "ImdbId",
    "tmdb": "TmdbId",
    "tmdbid": "TmdbId",
    "tvdb": "TvdbId",
    "tvdbid": "TvdbId",
    "tvmaze": "TvMazeId",
    "tvmazeid": "TvMazeId",
    "trakt": "TraktId",
    "traktid": "TraktId",
    "douban": "DoubanId",
    "doubanid": "DoubanId",
}


def build_prowlarr_search_params(request: SearchRequest) -> dict[str, Any]:
    query_parts = []

    identifier_query = _format_identifier_for_prowlarr(request.identifier)
    if identifier_query:
        query_parts.append(identifier_query)

    normalized_query = (
        normalize_user_message(request.query)
        if request.query and request.query.strip()
        else None
    )
    if normalized_query:
        query_identifier = _format_identifier_for_prowlarr(normalized_query)
        if query_identifier and not identifier_query:
            query_parts.append(query_identifier)
        else:
            query_parts.append(normalized_query)

    if not query_parts:
        raise ValueError("Either identifier or query is required")

    params: dict[str, Any] = {
        "query": " ".join(query_parts),
        "type": "search",
        "limit": PROWLARR_UPSTREAM_LIMIT,
        "offset": 0,
    }
    if request.categories:
        params["categories"] = request.categories
    if request.indexer_ids:
        params["indexerIds"] = request.indexer_ids
    return params


def normalize_search_results(
    raw_results: list[dict[str, Any]],
    *,
    prowlarr_url: str,
    prowlarr_download_url: str | None = None,
    prowlarr_api_key: str,
) -> list[SearchResult]:
    normalized: list[SearchResult] = []
    seen_links: set[str] = set()

    for raw_result in raw_results:
        download_link = _extract_download_link(
            raw_result,
            prowlarr_url=prowlarr_url,
            prowlarr_download_url=prowlarr_download_url,
            prowlarr_api_key=prowlarr_api_key,
        )
        if not download_link:
            continue

        dedupe_key = download_link.casefold()
        if dedupe_key in seen_links:
            continue

        seen_links.add(dedupe_key)
        normalized.append(
            SearchResult(
                title=str(_pick(raw_result, "title", "fileName", "file_name") or "Untitled"),
                download_link=download_link,
                size=_to_int(_pick(raw_result, "size")),
                seeders=_to_int(_pick(raw_result, "seeders")),
                leechers=_to_int(_pick(raw_result, "leechers")),
                grabs=_to_int(_pick(raw_result, "grabs")),
                indexer=_to_optional_str(_pick(raw_result, "indexer")),
                protocol=_to_optional_str(_pick(raw_result, "protocol")),
                publish_date=_to_optional_str(_pick(raw_result, "publishDate", "publish_date")),
                info_hash=_to_optional_str(_pick(raw_result, "infoHash", "info_hash")),
            )
        )

    return normalized


def _format_identifier_for_prowlarr(identifier: str | None) -> str | None:
    if not identifier or not identifier.strip():
        return None

    value = normalize_user_message(identifier)
    if "{" in value and "}" in value:
        return value

    imdb_id = extract_imdb_id(value)
    if imdb_id and (_is_imdb_reference(value) or re.fullmatch(r"tt\d{6,12}", value, flags=re.IGNORECASE)):
        return imdb_id

    if ":" in value:
        prefix, raw_identifier = value.split(":", 1)
        param_name = IDENTIFIER_PARAM_NAMES.get(prefix.strip().lower())
        raw_identifier = raw_identifier.strip()
        if param_name == "ImdbId" and raw_identifier:
            return raw_identifier
        if param_name and raw_identifier:
            return f"{{{param_name}:{raw_identifier}}}"

    return value


def _extract_download_link(
    raw_result: dict[str, Any],
    *,
    prowlarr_url: str,
    prowlarr_download_url: str | None,
    prowlarr_api_key: str,
) -> str | None:
    raw_link = _pick_download_link(raw_result)
    if not isinstance(raw_link, str) or not raw_link.strip():
        return None

    return _normalize_result_link(
        raw_link.strip(),
        prowlarr_url=prowlarr_url,
        prowlarr_download_url=prowlarr_download_url,
        prowlarr_api_key=prowlarr_api_key,
    )


def _pick_download_link(raw_result: dict[str, Any]) -> str | None:
    candidates = [
        _pick(raw_result, "magnetUrl", "magnet_url"),
        _pick(raw_result, "guid"),
        _pick(raw_result, "downloadUrl", "download_url"),
    ]
    string_candidates = [value.strip() for value in candidates if isinstance(value, str) and value.strip()]

    for value in string_candidates:
        if urlparse(value).scheme.lower() in {"magnet", "bc"}:
            return value

    download_url = _pick(raw_result, "downloadUrl", "download_url")
    if isinstance(download_url, str) and download_url.strip():
        return download_url.strip()

    return string_candidates[0] if string_candidates else None


def _normalize_result_link(
    link: str,
    *,
    prowlarr_url: str,
    prowlarr_download_url: str | None,
    prowlarr_api_key: str,
) -> str | None:
    parsed = urlparse(link)
    download_base_url = (prowlarr_download_url or prowlarr_url).rstrip("/")

    if parsed.scheme.lower() in {"magnet", "bc"}:
        return normalize_download_link(link)

    if not parsed.scheme and link.startswith("/"):
        link = urljoin(f"{download_base_url}/", link)
        parsed = urlparse(link)

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    base_netloc = urlparse(prowlarr_url).netloc
    download_base = urlparse(download_base_url)
    if parsed.netloc == base_netloc and download_base.netloc and parsed.netloc != download_base.netloc:
        parsed = parsed._replace(scheme=download_base.scheme or parsed.scheme, netloc=download_base.netloc)
        link = urlunparse(parsed)

    return normalize_download_link(link)


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_imdb_reference(value: str) -> bool:
    return "imdb.com/title/" in value.casefold()
