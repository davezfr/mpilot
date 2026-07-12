from __future__ import annotations

import re
import unicodedata

from mpilot.acquisition.models import SearchResult


_RELEASE_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


def normalize_title_tokens(value: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(character for character in decomposed if not unicodedata.combining(character))
    return [token for token in re.findall(r"\w+", normalized, flags=re.UNICODE) if token]


def validate_complementary_results(
    results: list[SearchResult],
    *,
    canonical_title: str,
    year: int,
) -> list[SearchResult]:
    title_tokens = normalize_title_tokens(canonical_title)
    expected_year = str(year)
    validated: list[SearchResult] = []
    seen_links: set[str] = set()
    seen_hashes: set[str] = set()

    if not title_tokens or not _RELEASE_YEAR_RE.fullmatch(expected_year):
        return []

    for result in results:
        tokens = normalize_title_tokens(result.title)
        years = {token for token in tokens if _RELEASE_YEAR_RE.fullmatch(token)}
        if expected_year not in years or any(candidate != expected_year for candidate in years):
            continue
        if not _tokens_appear_in_order(title_tokens, tokens):
            continue

        normalized_link = unicodedata.normalize("NFKC", result.download_link).strip().casefold()
        normalized_hash = result.info_hash.strip().casefold() if result.info_hash and result.info_hash.strip() else None
        if normalized_link in seen_links or (normalized_hash is not None and normalized_hash in seen_hashes):
            continue
        seen_links.add(normalized_link)
        if normalized_hash is not None:
            seen_hashes.add(normalized_hash)
        validated.append(result)

    return validated


def _tokens_appear_in_order(expected: list[str], actual: list[str]) -> bool:
    cursor = iter(actual)
    return all(any(token == candidate for candidate in cursor) for token in expected)
