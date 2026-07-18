from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Literal

from mpilot.acquisition.models import SearchResult


MediaType = Literal["movie", "tv"]

_RELEASE_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_COLLECTION_MARKER_RE = re.compile(
    r"\b(?:collection|pack|anthology|trilogy|quadrilogy|filmography|box\s+set|"
    r"top\s+\d+|complete\s+(?:collection|movies?|films?))\b",
    flags=re.IGNORECASE,
)
DOWNLOADABLE_VERIFICATION_STATUSES = {"imdb_verified", "title_year_validated"}


@dataclass(frozen=True)
class ImdbIdentityValidation:
    verified_results: list[SearchResult]
    rejection_counts: dict[str, int]

    @property
    def rejected_count(self) -> int:
        return sum(self.rejection_counts.values())


def normalize_title_tokens(value: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(character for character in decomposed if not unicodedata.combining(character))
    return [token for token in re.findall(r"\w+", normalized, flags=re.UNICODE) if token]


def validate_imdb_results(
    results: list[SearchResult],
    *,
    imdb_id: str,
    canonical_title: str,
    title_aliases: Iterable[str] | None,
    year: int | None,
    media_type: MediaType,
) -> ImdbIdentityValidation:
    aliases = _identity_alias_tokens(canonical_title, title_aliases)
    expected_imdb_id = imdb_id.strip().casefold()
    rejection_counts: Counter[str] = Counter()
    verified: list[SearchResult] = []

    if not aliases or not re.fullmatch(r"tt\d{6,12}", expected_imdb_id):
        return ImdbIdentityValidation(
            verified_results=[],
            rejection_counts={"identity_metadata_incomplete": len(results)},
        )
    if media_type == "movie" and not _valid_release_year(year):
        return ImdbIdentityValidation(
            verified_results=[],
            rejection_counts={"identity_metadata_incomplete": len(results)},
        )

    for result in results:
        source_imdb_id = result.source_imdb_id.strip().casefold() if result.source_imdb_id else None
        if source_imdb_id and source_imdb_id != expected_imdb_id:
            rejection_counts["source_imdb_mismatch"] += 1
            continue

        title_tokens = normalize_title_tokens(result.title)
        matched_alias = _matching_alias_tokens(aliases, title_tokens)
        if matched_alias is None:
            rejection_counts["title_mismatch"] += 1
            continue

        if media_type == "movie":
            year_rejection = _movie_year_rejection(title_tokens, matched_alias, expected_year=int(year))
            if year_rejection:
                rejection_counts[year_rejection] += 1
                continue
            if _remaining_title_has_collection_marker(title_tokens, matched_alias):
                rejection_counts["collection_marker"] += 1
                continue
            reason = "source_imdb_title_year" if source_imdb_id else "title_year"
        else:
            reason = "source_imdb_title" if source_imdb_id else "title"

        verified.append(
            result.model_copy(
                update={
                    "verification_status": "imdb_verified",
                    "verification_reason": reason,
                }
            )
        )

    return ImdbIdentityValidation(
        verified_results=verified,
        rejection_counts=dict(sorted(rejection_counts.items())),
    )


def validate_title_year(
    title: str,
    *,
    canonical_title: str,
    title_aliases: Iterable[str] | None,
    year: int,
) -> str | None:
    aliases = _identity_alias_tokens(canonical_title, title_aliases)
    title_tokens = normalize_title_tokens(title)
    matched_alias = _matching_alias_tokens(aliases, title_tokens)
    if matched_alias is None:
        return None
    if _movie_year_rejection(title_tokens, matched_alias, expected_year=year):
        return None
    if _remaining_title_has_collection_marker(title_tokens, matched_alias):
        return None
    return "title_year"


def is_downloadable_snapshot_result(result: SearchResult) -> bool:
    return result.verification_status in DOWNLOADABLE_VERIFICATION_STATUSES


def _identity_alias_tokens(
    canonical_title: str,
    title_aliases: Iterable[str] | None,
) -> list[list[str]]:
    values = [canonical_title, *(title_aliases or [])]
    aliases: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        tokens = normalize_title_tokens(str(value))
        key = tuple(tokens)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(tokens)
    aliases.sort(key=lambda tokens: (len(tokens), sum(len(token) for token in tokens)), reverse=True)
    return aliases


def _matching_alias_tokens(aliases: list[list[str]], actual: list[str]) -> list[str] | None:
    for alias in aliases:
        if _tokens_appear_contiguously(alias, actual):
            return alias
    return None


def _tokens_appear_contiguously(expected: list[str], actual: list[str]) -> bool:
    width = len(expected)
    return any(actual[start : start + width] == expected for start in range(len(actual) - width + 1))


def _movie_year_rejection(
    title_tokens: list[str],
    matched_alias: list[str],
    *,
    expected_year: int,
) -> str | None:
    if not _valid_release_year(expected_year):
        return "identity_metadata_incomplete"
    alias_years = {token for token in matched_alias if _RELEASE_YEAR_RE.fullmatch(token)}
    release_years = {
        token for token in title_tokens if _RELEASE_YEAR_RE.fullmatch(token) and token not in alias_years
    }
    expected = str(expected_year)
    if expected not in release_years:
        return "release_year_missing" if not release_years else "release_year_mismatch"
    if any(candidate != expected for candidate in release_years):
        return "conflicting_release_years"
    return None


def _remaining_title_has_collection_marker(title_tokens: list[str], matched_alias: list[str]) -> bool:
    remaining = _tokens_without_ordered_match(title_tokens, matched_alias)
    return bool(_COLLECTION_MARKER_RE.search(" ".join(remaining)))


def _tokens_without_ordered_match(actual: list[str], expected: list[str]) -> list[str]:
    matched_indexes: set[int] = set()
    expected_index = 0
    for actual_index, token in enumerate(actual):
        if expected_index >= len(expected):
            break
        if token == expected[expected_index]:
            matched_indexes.add(actual_index)
            expected_index += 1
    return [token for index, token in enumerate(actual) if index not in matched_indexes]


def _valid_release_year(value: int | None) -> bool:
    return value is not None and bool(_RELEASE_YEAR_RE.fullmatch(str(value)))
