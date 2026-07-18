from __future__ import annotations

import unicodedata

from mpilot.acquisition.domain.imdb_identity import validate_title_year
from mpilot.acquisition.models import SearchResult


def validate_complementary_results(
    results: list[SearchResult],
    *,
    canonical_title: str,
    year: int,
    title_aliases: list[str] | None = None,
) -> list[SearchResult]:
    validated: list[SearchResult] = []
    seen_links: set[str] = set()
    seen_hashes: set[str] = set()

    for result in results:
        reason = validate_title_year(
            result.title,
            canonical_title=canonical_title,
            title_aliases=title_aliases,
            year=year,
        )
        if reason is None:
            continue

        normalized_link = unicodedata.normalize("NFKC", result.download_link).strip().casefold()
        normalized_hash = result.info_hash.strip().casefold() if result.info_hash and result.info_hash.strip() else None
        if normalized_link in seen_links or (normalized_hash is not None and normalized_hash in seen_hashes):
            continue
        seen_links.add(normalized_link)
        if normalized_hash is not None:
            seen_hashes.add(normalized_hash)
        validated.append(
            result.model_copy(
                update={
                    "verification_status": "title_year_validated",
                    "verification_reason": reason,
                }
            )
        )

    return validated
