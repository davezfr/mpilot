from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from mpilot.acquisition.config import Settings
from mpilot.acquisition.domain.quality import extract_external_movie_id, normalize_user_message
from mpilot.acquisition.exceptions import UpstreamServiceError


logger = logging.getLogger("qbitlarr-api.wikidata")

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_MWAPI_ENDPOINT = "www.wikidata.org"
SOURCE_PROPERTY_IDS = {
    "douban": "P4529",
    "allocine": "P1265",
}

# Publication-date statements (P577) can repeat per release region, multiplying
# rows per film, so the query fetches a generous row budget and dedupes by IMDb
# ID client-side down to the caller's candidate limit.
MOVIE_CANDIDATE_ROW_LIMIT = 30

# Only surface items that are (a subclass of) a film, TV series, or TV film, so
# entity search cannot hand back individual episodes or specials as candidates.
MOVIE_CANDIDATE_TYPE_QIDS = ("Q11424", "Q5398426", "Q506240")
TV_TYPE_QIDS = {"Q5398426"}

# A trailing release year (e.g. "Parasite 2019") defeats Wikidata's label-based
# entity search. It is stripped only as a retry when the verbatim query finds
# nothing, so legitimate year-in-title films ("1917", "Blade Runner 2049") are
# left untouched.
_TRAILING_YEAR_RE = re.compile(r"[\s(\[]*\b(?:19|20)\d{2}\b[\s)\]]*$")


async def resolve_external_movie_id(
    user_message: str,
    settings: Settings,
) -> dict[str, str | None] | None:
    source = _detect_external_source(user_message)
    if source is None:
        return None

    external = extract_external_movie_id(user_message)
    if external is None:
        return _unresolved_resolution(source=source, source_id=None)

    payload = await _query_wikidata_imdb(
        property_id=SOURCE_PROPERTY_IDS[source],
        source_id=external["source_id"],
        settings=settings,
    )
    parsed = _parse_imdb_resolution(payload)
    if parsed is None:
        return _unresolved_resolution(source=source, source_id=external["source_id"])

    return {
        "source": source,
        "source_id": external["source_id"],
        "imdb_id": parsed["imdb_id"],
        "wikidata_qid": parsed["wikidata_qid"],
    }


async def search_movie_candidates(
    query: str,
    settings: Settings,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Resolve a free-text title to candidate films/TV shows via Wikidata.

    Wikidata entity search finds items that carry an IMDb *title* ID (``tt...``),
    so a keyword can be locked to a concrete movie/show before the normal
    IMDb-based release search runs. Returns up to ``limit`` candidates as
    ``{"title", "year", "imdb_id", "wikidata_qid", "media_type"}`` dicts, ordered by Wikidata
    search relevance. Each candidate includes a canonical ``media_type`` so
    clients can distinguish movies from TV series before the user chooses an
    IMDb identity.

    Returns an empty list when nothing matches or on any upstream error. The
    caller is then expected to ask the user for an explicit IMDb link or ID.
    Wikidata's weaker fuzzy-search recall is a deliberate trade for a
    zero-setup, no-API-key resolver.
    """
    cleaned = normalize_user_message(query).strip()
    if not cleaned:
        return []

    payload = await _run_sparql(_build_movie_candidate_query(cleaned), settings)
    candidates = _parse_movie_candidates(payload, limit=limit)
    if candidates:
        return candidates

    without_year = _strip_trailing_year(cleaned)
    if without_year and without_year != cleaned:
        payload = await _run_sparql(_build_movie_candidate_query(without_year), settings)
        candidates = _parse_movie_candidates(payload, limit=limit)

    return candidates


async def resolve_imdb_metadata(imdb_id: str, settings: Settings) -> dict[str, Any] | None:
    """Resolve canonical English title, year, and media type for an IMDb title ID."""
    normalized = imdb_id.strip().lower()
    if not re.fullmatch(r"tt\d{6,12}", normalized):
        return None

    type_values = " ".join(f"wd:{qid}" for qid in MOVIE_CANDIDATE_TYPE_QIDS)
    query = (
        "SELECT ?item ?itemLabel ?alias ?originalTitle ?year ?type WHERE { "
        f'?item wdt:P345 "{normalized}" . '
        "?item wdt:P31/wdt:P279* ?type . "
        f"VALUES ?type {{ {type_values} }} "
        "OPTIONAL { ?item wdt:P577 ?date . BIND(YEAR(?date) AS ?year) } "
        'OPTIONAL { ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "en") } '
        "OPTIONAL { ?item wdt:P1476 ?originalTitle . } "
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . } '
        "} ORDER BY ?year LIMIT 20"
    )
    return _parse_imdb_metadata(await _run_sparql(query, settings, raise_on_error=True), imdb_id=normalized)


def _parse_imdb_metadata(payload: dict[str, Any] | None, *, imdb_id: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    bindings = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(bindings, list):
        return None

    title: str | None = None
    qid: str | None = None
    years: list[int] = []
    aliases: set[str] = set()
    media_type = "movie"
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        candidate_qid = _wikidata_qid(_binding_value(binding.get("item")))
        candidate_title = _binding_value(binding.get("itemLabel"))
        if candidate_title and candidate_title != candidate_qid:
            title = title or candidate_title
        alias = _binding_value(binding.get("alias"))
        if alias:
            aliases.add(alias)
        original_title = _binding_value(binding.get("originalTitle"))
        if original_title:
            aliases.add(original_title)
        qid = qid or candidate_qid
        year = _parse_year(_binding_value(binding.get("year")))
        if year is not None:
            years.append(year)
        type_qid = _wikidata_qid(_binding_value(binding.get("type")))
        if type_qid in TV_TYPE_QIDS:
            media_type = "tv"

    if not title or not years:
        return None
    aliases.discard(title)
    return {
        "imdb_id": imdb_id,
        "canonical_title": title,
        "title_aliases": sorted(aliases, key=str.casefold),
        "year": min(years),
        "media_type": media_type,
        "metadata_source": "wikidata",
        "wikidata_qid": qid,
    }


async def _query_wikidata_imdb(
    *,
    property_id: str,
    source_id: str,
    settings: Settings,
) -> dict[str, Any] | None:
    query = (
        "SELECT ?item ?imdb WHERE { "
        f'?item wdt:{property_id} "{source_id}" . '
        "?item wdt:P345 ?imdb . "
        "?item wdt:P31/wdt:P279* wd:Q11424 . "
        "}"
    )
    return await _run_sparql(query, settings)


async def _run_sparql(
    query: str,
    settings: Settings,
    *,
    raise_on_error: bool = False,
) -> dict[str, Any] | None:
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": "qBitlarr/0.1 (external-movie-resolver)",
    }
    params = {"query": query, "format": "json"}

    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(WIKIDATA_SPARQL_URL, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Wikidata query failed with HTTP %s", exc.response.status_code)
        if raise_on_error:
            raise UpstreamServiceError(f"Wikidata query failed with HTTP {exc.response.status_code}") from exc
        return None
    except httpx.RequestError as exc:
        logger.warning("Wikidata query failed: %s", exc.__class__.__name__)
        if raise_on_error:
            raise UpstreamServiceError("Wikidata is unreachable") from exc
        return None
    except ValueError:
        logger.warning("Wikidata returned invalid JSON")
        if raise_on_error:
            raise UpstreamServiceError("Wikidata returned invalid JSON")
        return None

    if not isinstance(payload, dict):
        if raise_on_error:
            raise UpstreamServiceError("Wikidata returned an unexpected response shape")
        return None
    return payload


def _build_movie_candidate_query(search_text: str) -> str:
    escaped = _escape_sparql_string(search_text)
    type_values = " ".join(f"wd:{qid}" for qid in MOVIE_CANDIDATE_TYPE_QIDS)
    return (
        "SELECT ?item ?itemLabel ?imdb ?year ?ordinal ?type WHERE { "
        "SERVICE wikibase:mwapi { "
        'bd:serviceParam wikibase:api "EntitySearch" . '
        f'bd:serviceParam wikibase:endpoint "{WIKIDATA_MWAPI_ENDPOINT}" . '
        f'bd:serviceParam mwapi:search "{escaped}" . '
        'bd:serviceParam mwapi:language "en" . '
        "?item wikibase:apiOutputItem mwapi:item . "
        "?item wikibase:apiOrdinal ?ordinal . "
        "} "
        "?item wdt:P345 ?imdb . "
        'FILTER(STRSTARTS(?imdb, "tt")) '
        "?item wdt:P31/wdt:P279* ?type . "
        f"VALUES ?type {{ {type_values} }} "
        "OPTIONAL { ?item wdt:P577 ?date . BIND(YEAR(?date) AS ?year) } "
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . } '
        "} "
        f"ORDER BY ?ordinal LIMIT {MOVIE_CANDIDATE_ROW_LIMIT}"
    )


def _escape_sparql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _strip_trailing_year(text: str) -> str:
    return _TRAILING_YEAR_RE.sub("", text).strip()


def _parse_movie_candidates(payload: dict[str, Any] | None, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    results = payload.get("results")
    if not isinstance(results, dict):
        return []

    bindings = results.get("bindings")
    if not isinstance(bindings, list):
        return []

    candidates_by_imdb: dict[str, dict[str, Any]] = {}
    candidate_order: list[str] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue

        imdb = _binding_value(binding.get("imdb"))
        if not imdb or not imdb.startswith("tt"):
            continue

        qid = _wikidata_qid(_binding_value(binding.get("item")))
        title = _binding_value(binding.get("itemLabel"))
        # The label service echoes the bare QID when an item has no label in the
        # requested language; such a row is useless to show the user, so skip it.
        if not title or title == qid:
            continue

        year = _parse_year(_binding_value(binding.get("year")))
        type_qid = _wikidata_qid(_binding_value(binding.get("type")))
        media_type = "tv" if type_qid in TV_TYPE_QIDS else "movie"
        existing = candidates_by_imdb.get(imdb)
        if existing is None:
            candidates_by_imdb[imdb] = {
                "title": title,
                "year": year,
                "imdb_id": imdb,
                "wikidata_qid": qid,
                "media_type": media_type,
            }
            candidate_order.append(imdb)
            continue

        # Publication dates and class ancestry can yield more than one row for
        # the same title. Preserve the earliest year and never downgrade a TV
        # classification if another matching ancestry row is encountered.
        existing_year = existing.get("year")
        if year is not None and (existing_year is None or year < existing_year):
            existing["year"] = year
        if media_type == "tv":
            existing["media_type"] = "tv"

    return [candidates_by_imdb[imdb] for imdb in candidate_order[:limit]]


def _parse_year(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_imdb_resolution(payload: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None

    results = payload.get("results")
    if not isinstance(results, dict):
        return None

    bindings = results.get("bindings")
    if not isinstance(bindings, list):
        return None

    matches: set[tuple[str, str]] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue

        imdb = _binding_value(binding.get("imdb"))
        item = _binding_value(binding.get("item"))
        qid = _wikidata_qid(item)
        if imdb and qid:
            matches.add((imdb, qid))

    if len(matches) != 1:
        return None

    imdb_id, wikidata_qid = next(iter(matches))
    return {"imdb_id": imdb_id, "wikidata_qid": wikidata_qid}


def _binding_value(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("value")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _wikidata_qid(item_url: str | None) -> str | None:
    if not item_url:
        return None
    candidate = item_url.rstrip("/").rsplit("/", 1)[-1]
    if candidate.startswith("Q") and candidate[1:].isdigit():
        return candidate
    return None


def _detect_external_source(user_message: str) -> str | None:
    normalized = normalize_user_message(user_message)
    if ":" in normalized:
        prefix, _ = normalized.split(":", 1)
        if prefix.strip().casefold() in {"douban", "allocine"}:
            return prefix.strip().casefold()

    parsed = urlparse(normalized)
    host = parsed.netloc.rsplit("@", 1)[-1].split(":", 1)[0].casefold()
    path = unquote(parsed.path)

    if host in {"movie.douban.com", "m.douban.com"} and "/subject/" in path:
        return "douban"
    if host == "allocine.fr" or host.endswith(".allocine.fr"):
        if "/film/" in path or "/series/" in path:
            return "allocine"
    return None


def _unresolved_resolution(*, source: str, source_id: str | None) -> dict[str, str | None]:
    return {
        "source": source,
        "source_id": source_id,
        "imdb_id": None,
        "wikidata_qid": None,
    }
