from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlparse

from mpilot.acquisition.models import SearchResult


MediaType = Literal["movie", "tv"]

MIN_AUTO_DOWNLOAD_SEEDERS = 5
DEFAULT_PREFER_RESOLUTION = "1080p"
DEFAULT_PREFER_SOURCE = "WEB-DL"
DEFAULT_PREFER_CODEC = "H.264"


@dataclass(frozen=True)
class QualityPreferences:
    resolution: str = DEFAULT_PREFER_RESOLUTION
    source: str = DEFAULT_PREFER_SOURCE
    codec: str = DEFAULT_PREFER_CODEC
    min_seeders: int = MIN_AUTO_DOWNLOAD_SEEDERS


DEFAULT_QUALITY_PREFERENCES = QualityPreferences()


_PREMIUM_REQUEST_RE = re.compile(r"\b(4k|2160p|uhd|remux)\b", re.IGNORECASE)
_REQUESTED_RESOLUTION_RE = re.compile(r"\b(2160\s*p|4k|uhd|1080\s*p|720\s*p|480\s*p)\b", re.IGNORECASE)
_IMDB_ID_RE = re.compile(r"\b(tt\d{6,12})\b", re.IGNORECASE)
_DOUBAN_SUBJECT_RE = re.compile(r"/subject/([1-9]\d*)/?", re.IGNORECASE)
_ALLOCINE_CFILM_RE = re.compile(r"fichefilm_gen_cfilm=([1-9]\d{0,9})\.html", re.IGNORECASE)
_TV_MARKER_RE = re.compile(
    r"\b(S\d{1,2}(?:E\d{1,3})?|Season\s+\d{1,2}|Complete\s+Season)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedQuality:
    resolution: str | None
    source: str | None
    codec: str | None
    is_remux: bool
    is_amzn: bool

    @property
    def is_premium(self) -> bool:
        return self.resolution == "2160p" or self.is_remux


def extract_imdb_id(user_message: str) -> str | None:
    for candidate in (normalize_user_message(user_message), _decode_url_text(user_message.strip())):
        match = _IMDB_ID_RE.search(candidate)
        if match:
            return match.group(1).lower()
    return None


def extract_external_movie_id(user_message: str) -> dict[str, str] | None:
    normalized = normalize_user_message(user_message)

    prefixed = _extract_prefixed_external_movie_id(normalized)
    if prefixed is not None:
        return prefixed

    parsed = urlparse(normalized)
    host = _hostname(parsed.netloc)
    path = unquote(parsed.path)

    if _is_douban_host(host):
        match = _DOUBAN_SUBJECT_RE.search(path)
        if match:
            return {"source": "douban", "source_id": match.group(1)}

    if _is_allocine_host(host):
        match = _ALLOCINE_CFILM_RE.search(path)
        if match:
            return {"source": "allocine", "source_id": match.group(1)}

    return None


def normalize_user_message(user_message: str) -> str:
    text = _strip_url_wrappers(user_message)
    parsed = urlparse(_decode_url_text(text))

    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        host = _hostname(parsed.netloc)
        path = unquote(parsed.path)
        imdb_match = _IMDB_ID_RE.search(path)
        if _is_imdb_host(host) and imdb_match:
            return f"https://www.imdb.com/title/{imdb_match.group(1).lower()}"

        return parsed.geturl()

    return re.sub(r"\s+", " ", text)


def contains_premium_quality_request(user_message: str) -> bool:
    return bool(_PREMIUM_REQUEST_RE.search(user_message))


def extract_requested_resolution(user_message: str) -> str | None:
    match = _REQUESTED_RESOLUTION_RE.search(user_message)
    if not match:
        return None

    requested = re.sub(r"\s+", "", match.group(1)).casefold()
    if requested in {"4k", "uhd", "2160p"}:
        return "2160p"
    return requested


def infer_media_type(user_message: str, results: list[SearchResult] | None = None) -> MediaType:
    if _TV_MARKER_RE.search(user_message):
        return "tv"

    for result in results or []:
        if _TV_MARKER_RE.search(result.title):
            return "tv"

    return "movie"


def parse_quality(title: str) -> ParsedQuality:
    normalized = _normalize_release_text(title)

    resolution = None
    if re.search(r"\b(2160p|4k|uhd)\b", normalized, flags=re.IGNORECASE):
        resolution = "2160p"
    elif re.search(r"\b1080p\b", normalized, flags=re.IGNORECASE):
        resolution = "1080p"
    elif re.search(r"\b720p\b", normalized, flags=re.IGNORECASE):
        resolution = "720p"
    elif re.search(r"\b480p\b", normalized, flags=re.IGNORECASE):
        resolution = "480p"

    is_remux = bool(re.search(r"\bremux\b", normalized, flags=re.IGNORECASE))
    is_amzn = bool(re.search(r"\b(amzn|amazon)\b", normalized, flags=re.IGNORECASE))

    source = None
    if re.search(r"\bweb\s*dl\b|\bwebdl\b", normalized, flags=re.IGNORECASE):
        source = "WEB-DL"
    elif re.search(r"\bweb\s*rip\b|\bwebrip\b", normalized, flags=re.IGNORECASE):
        source = "WEBRip"
    elif re.search(r"\bhdtv\b", normalized, flags=re.IGNORECASE):
        source = "HDTV"
    elif is_remux or re.search(r"\b(blu\s*ray|bluray|bdrip|brrip|bdremux)\b", normalized, flags=re.IGNORECASE):
        source = "BluRay"

    codec = None
    if re.search(r"\b(h\s*264|x264|avc)\b", normalized, flags=re.IGNORECASE):
        codec = "H.264"
    elif re.search(r"\b(h\s*265|x265|hevc)\b", normalized, flags=re.IGNORECASE):
        codec = "H.265"

    return ParsedQuality(
        resolution=resolution,
        source=source,
        codec=codec,
        is_remux=is_remux,
        is_amzn=is_amzn,
    )


def format_quality(parsed: ParsedQuality) -> str:
    parts: list[str] = []
    if parsed.resolution:
        parts.append(parsed.resolution)

    if parsed.is_remux and parsed.source in {None, "BluRay"}:
        parts.append("REMUX")
    elif parsed.source:
        parts.append(parsed.source)

    if parsed.codec:
        parts.append(parsed.codec)

    return " ".join(parts) if parts else "Unknown quality"


def format_choice_label(parsed: ParsedQuality, *, default_resolution: str = "1080p") -> str:
    """Compact release label for identity-certain result lists.

    When every result is known to be the same film (IMDb-resolved search),
    the title and the default resolution carry no information, so the label
    keeps only the attributes that differ between releases: resolution when
    it deviates from the default, source, and codec.
    """
    parts: list[str] = []
    if parsed.resolution and parsed.resolution != default_resolution:
        parts.append(parsed.resolution)

    if parsed.is_remux and parsed.source in {None, "BluRay"}:
        parts.append("REMUX")
    elif parsed.source:
        parts.append(parsed.source)

    if parsed.codec:
        parts.append(parsed.codec)

    return " · ".join(parts) if parts else "Unknown quality"


def calculate_score(
    result: SearchResult,
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None = None,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
) -> int | None:
    seeders = result.seeders or 0
    if seeders < preferences.min_seeders:
        return None

    base_score = calculate_quality_preference(
        result,
        media_type=media_type,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        preferences=preferences,
    )
    if base_score is None:
        return None

    return base_score + min(seeders, 99)


def calculate_quality_preference(
    result: SearchResult,
    *,
    media_type: MediaType,
    prefer_premium: bool,
    requested_resolution: str | None = None,
    preferences: QualityPreferences = DEFAULT_QUALITY_PREFERENCES,
) -> int | None:
    parsed = parse_quality(result.title)
    if not _matches_quality_request(
        parsed,
        prefer_premium=prefer_premium,
        requested_resolution=requested_resolution,
        preferences=preferences,
    ):
        return None

    if prefer_premium:
        base_score = _premium_score(parsed)
    elif media_type == "tv":
        base_score = _tv_score(parsed, preferences)
    else:
        base_score = _movie_score(parsed, preferences)

    return base_score


def clean_display_title(title: str) -> str:
    text = re.sub(r"[\._]+", " ", title).strip()
    match = re.search(
        r"\b(2160p|1080p|720p|480p|4k|uhd|web\s*dl|webdl|web\s*rip|webrip|hdtv|blu\s*ray|bluray|remux|h\s*26[45]|x26[45]|hevc|avc)\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        text = text[: match.start()].strip(" ._-")

    text = re.sub(r"\s+", " ", text)
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if year_match and "(" not in text[year_match.start() : year_match.end() + 2]:
        year = year_match.group(1)
        name = (text[: year_match.start()] + text[year_match.end() :]).strip(" -")
        if name:
            text = f"{name} ({year})"

    return text or title


def _premium_score(parsed: ParsedQuality) -> int:
    score = 3000
    if parsed.resolution == "2160p":
        score = 6000
    elif parsed.is_remux:
        score = 5800

    if parsed.is_remux:
        score += 250
    score += _source_points(parsed)
    score += _codec_points(parsed)
    if parsed.is_amzn:
        score += 10
    return score


def _matches_quality_request(
    parsed: ParsedQuality,
    *,
    prefer_premium: bool,
    requested_resolution: str | None,
    preferences: QualityPreferences,
) -> bool:
    if requested_resolution:
        return parsed.resolution == requested_resolution

    if prefer_premium:
        return parsed.is_premium

    return parsed.resolution == preferences.resolution


def _movie_score(parsed: ParsedQuality, prefs: QualityPreferences) -> int:
    alt_codec = "H.265" if prefs.codec == "H.264" else "H.264"
    if parsed.resolution == prefs.resolution and parsed.source == prefs.source and parsed.codec == prefs.codec:
        return 5000
    if parsed.resolution == prefs.resolution and parsed.source == prefs.source and parsed.codec == alt_codec:
        return 4900
    if parsed.resolution == prefs.resolution and parsed.source == "WEBRip" and parsed.codec == prefs.codec and prefs.source != "WEBRip":
        return 4800
    if parsed.resolution == prefs.resolution and (parsed.source == "BluRay" or parsed.is_remux):
        return 4700
    if parsed.resolution == prefs.resolution:
        return 4600 + _source_points(parsed) + _codec_points(parsed)
    return 3000 + _resolution_points(parsed) + _source_points(parsed) + _codec_points(parsed)


def _tv_score(parsed: ParsedQuality, prefs: QualityPreferences) -> int:
    alt_codec = "H.265" if prefs.codec == "H.264" else "H.264"
    if (
        parsed.resolution == prefs.resolution
        and parsed.is_amzn
        and parsed.source == prefs.source
        and parsed.codec == prefs.codec
    ):
        return 5000
    if (
        parsed.resolution == prefs.resolution
        and parsed.is_amzn
        and parsed.source == prefs.source
        and parsed.codec == alt_codec
    ):
        return 4900
    if parsed.resolution == prefs.resolution and parsed.source == prefs.source and parsed.codec == prefs.codec:
        return 4800
    if parsed.resolution == prefs.resolution and parsed.source == prefs.source and parsed.codec == alt_codec:
        return 4700
    if parsed.resolution == prefs.resolution:
        return 4600 + _source_points(parsed) + _codec_points(parsed)
    return 3000 + _resolution_points(parsed) + _source_points(parsed) + _codec_points(parsed)


def _resolution_points(parsed: ParsedQuality) -> int:
    return {
        "2160p": 90,
        "1080p": 80,
        "720p": 40,
        "480p": 10,
    }.get(parsed.resolution or "", 0)


def _source_points(parsed: ParsedQuality) -> int:
    if parsed.is_remux:
        return 25
    return {
        "WEB-DL": 80,
        "WEBRip": 60,
        "HDTV": 40,
        "BluRay": 20,
    }.get(parsed.source or "", 0)


def _codec_points(parsed: ParsedQuality) -> int:
    return {
        "H.264": 20,
        "H.265": 10,
    }.get(parsed.codec or "", 0)


def _normalize_release_text(title: str) -> str:
    return re.sub(r"[\[\]()._\-]+", " ", title)


def _strip_url_wrappers(value: str) -> str:
    text = value.strip().replace("\u200b", "")
    wrapper_pairs = (("<", ">"), ("(", ")"), ("[", "]"), ('"', '"'), ("'", "'"))
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for left, right in wrapper_pairs:
            if text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()
                changed = True
                break
    return text.rstrip(".,;!")


def _decode_url_text(value: str) -> str:
    text = value
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _hostname(netloc: str) -> str:
    return netloc.rsplit("@", 1)[-1].split(":", 1)[0].casefold()


def _is_imdb_host(host: str) -> bool:
    return host == "imdb.com" or host.endswith(".imdb.com")


def _is_douban_host(host: str) -> bool:
    return host == "movie.douban.com" or host == "m.douban.com"


def _is_allocine_host(host: str) -> bool:
    return host == "allocine.fr" or host.endswith(".allocine.fr")


def _extract_prefixed_external_movie_id(value: str) -> dict[str, str] | None:
    if ":" not in value:
        return None

    prefix, raw_id = value.split(":", 1)
    source = prefix.strip().casefold()
    source_id = raw_id.strip()
    if source not in {"douban", "allocine"}:
        return None
    if not source_id.isdigit() or source_id.startswith("0"):
        return None
    return {"source": source, "source_id": source_id}
