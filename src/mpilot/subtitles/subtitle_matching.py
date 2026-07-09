from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .providers.base import SubtitleCandidate


HIGH_CONFIDENCE_SCORE = 80
MEDIUM_CONFIDENCE_SCORE = 40
DEFAULT_MATCH_PROVIDER_PRIORITY = ("subdl", "opensubtitles")

TECHNICAL_STOPWORDS = {
    "amzn",
    "bd",
    "bdrip",
    "bluray",
    "br",
    "brrip",
    "dl",
    "dvd",
    "dvdrip",
    "h264",
    "h265",
    "hevc",
    "nf",
    "remux",
    "rip",
    "web",
    "webdl",
    "webrip",
    "x264",
    "x265",
    "xvid",
}
TITLE_STOPWORDS = {"a", "an", "of", "the"}
EDITION_ALIASES = {
    "director": ("director", "directors"),
    "extended": ("extended",),
    "remastered": ("remastered",),
    "theatrical": ("theatrical",),
    "uncut": ("uncut",),
}


@dataclass(frozen=True)
class ReleaseInfo:
    raw: str
    title_tokens: Tuple[str, ...]
    year: Optional[int]
    season: Optional[int]
    episode: Optional[int]
    source_family: Optional[str]
    resolution: Optional[str]
    codec: Optional[str]
    group: Optional[str]
    editions: frozenset[str]


@dataclass(frozen=True)
class SubtitleMatchScore:
    candidate: SubtitleCandidate
    score: int
    confidence: str
    reasons: Tuple[str, ...]


def parse_release_info(value: str) -> ReleaseInfo:
    raw = str(value or "")
    stem = _release_stem(raw)
    tokens = _tokens(stem)
    year = _find_year(tokens)
    season, episode = _find_season_episode(stem)
    source_family = _source_family(stem, tokens)
    resolution = _first_match(tokens, re.compile(r"^[1-9][0-9]{2,3}p$"))
    codec = _codec(tokens)
    group = _release_group(stem)
    editions = frozenset(_edition_tokens(tokens))
    title_tokens = _title_tokens(tokens, year)
    return ReleaseInfo(
        raw=raw,
        title_tokens=title_tokens,
        year=year,
        season=season,
        episode=episode,
        source_family=source_family,
        resolution=resolution,
        codec=codec,
        group=group,
        editions=editions,
    )


def score_subtitle_candidate(media: ReleaseInfo, candidate: SubtitleCandidate) -> SubtitleMatchScore:
    subtitle = parse_release_info(_candidate_release_text(candidate))
    score = 0
    reasons: List[str] = []

    if _normalized_release(media.raw) == _normalized_release(subtitle.raw):
        score += 100
        reasons.append("exact_release_match")

    title_score = _title_score(media.title_tokens, subtitle.title_tokens)
    if title_score:
        score += title_score
        reasons.append("title_match:%s" % title_score)

    if media.year is not None and subtitle.year is not None:
        if media.year == subtitle.year:
            score += 20
            reasons.append("same_year:%s" % media.year)
        else:
            score -= 40
            reasons.append("year_mismatch:%s->%s" % (media.year, subtitle.year))

    score += _source_family_score(media, subtitle, reasons)
    score += _edition_score(media, subtitle, reasons)

    if media.resolution and subtitle.resolution and media.resolution == subtitle.resolution:
        score += 5
        reasons.append("same_resolution:%s" % media.resolution)

    if media.codec and subtitle.codec and media.codec == subtitle.codec:
        score += 3
        reasons.append("same_codec:%s" % media.codec)

    if media.group and subtitle.group and media.group == subtitle.group:
        score += 10
        reasons.append("same_release_group:%s" % media.group)

    return SubtitleMatchScore(
        candidate=candidate,
        score=score,
        confidence=confidence_for_score(score),
        reasons=tuple(reasons),
    )


def rank_subtitle_candidates(
    media: ReleaseInfo,
    candidates: Iterable[SubtitleCandidate],
    provider_priority: Sequence[str] = DEFAULT_MATCH_PROVIDER_PRIORITY,
) -> List[SubtitleMatchScore]:
    priority = {provider: index for index, provider in enumerate(provider_priority)}
    fallback_index = len(priority)
    scored = [score_subtitle_candidate(media, candidate) for candidate in candidates]
    return sorted(
        scored,
        key=lambda item: (-item.score, priority.get(item.candidate.provider, fallback_index), item.candidate.provider),
    )


def confidence_for_score(score: int) -> str:
    if score >= HIGH_CONFIDENCE_SCORE:
        return "high"
    if score >= MEDIUM_CONFIDENCE_SCORE:
        return "medium"
    return "low"


def _candidate_release_text(candidate: SubtitleCandidate) -> str:
    return candidate.release_name or candidate.file_name or candidate.provider_id


def _release_stem(value: str) -> str:
    name = Path(value).name
    for suffix in (".srt", ".ass", ".ssa", ".vtt", ".sub", ".mkv", ".mp4", ".avi", ".mov", ".zip"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _tokens(value: str) -> Tuple[str, ...]:
    return tuple(token for token in re.split(r"[^A-Za-z0-9]+", value.lower()) if token)


def _find_year(tokens: Sequence[str]) -> Optional[int]:
    for token in tokens:
        if re.fullmatch(r"(19|20)\d{2}", token):
            return int(token)
    return None


def _find_season_episode(stem: str) -> Tuple[Optional[int], Optional[int]]:
    value = stem.lower()
    match = re.search(r"(?:^|[^a-z0-9])s(\d{1,2})[^a-z0-9]*e(\d{1,3})(?:[^a-z0-9]|$)", value)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})(?:[^a-z0-9]|$)", value)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def _source_family(stem: str, tokens: Sequence[str]) -> Optional[str]:
    compact = re.sub(r"[^a-z0-9]+", "", stem.lower())
    token_set = set(tokens)
    if any(marker in compact for marker in ("bluray", "bdrip", "brrip", "remux")):
        return "bluray"
    if "web" in token_set or any(marker in compact for marker in ("webdl", "webrip")):
        return "web"
    if any(marker in compact for marker in ("dvdrip", "dvd")):
        return "dvd"
    return None


def _codec(tokens: Sequence[str]) -> Optional[str]:
    token_set = set(tokens)
    for codec in ("x264", "x265", "h264", "h265", "hevc", "xvid", "av1"):
        if codec in token_set:
            return codec
    return None


def _release_group(stem: str) -> Optional[str]:
    name = stem.strip()
    bracket_match = re.search(r"-\s*\[([^\]]+)\]\s*$", name)
    if bracket_match:
        return bracket_match.group(1).strip().lower()
    dash_match = re.search(r"-([A-Za-z0-9.]+)\s*$", name)
    if dash_match:
        return dash_match.group(1).strip().lower()
    return None


def _edition_tokens(tokens: Sequence[str]) -> List[str]:
    token_set = set(tokens)
    editions = []
    for name, aliases in EDITION_ALIASES.items():
        if any(alias in token_set for alias in aliases):
            editions.append(name)
    return editions


def _title_tokens(tokens: Sequence[str], year: Optional[int]) -> Tuple[str, ...]:
    selected = []
    for token in tokens:
        if year is not None and token == str(year):
            break
        if token in TITLE_STOPWORDS or token in TECHNICAL_STOPWORDS:
            continue
        if re.fullmatch(r"[1-9][0-9]{2,3}p", token):
            continue
        selected.append(token)
    return tuple(selected)


def _normalized_release(value: str) -> str:
    stem = _release_stem(value)
    return " ".join(_tokens(stem))


def _title_score(media_tokens: Sequence[str], subtitle_tokens: Sequence[str]) -> int:
    if not media_tokens or not subtitle_tokens:
        return 0
    media_set = set(media_tokens)
    subtitle_set = set(subtitle_tokens)
    if media_set == subtitle_set:
        return 40
    overlap = len(media_set & subtitle_set)
    denominator = max(len(media_set), len(subtitle_set))
    if denominator == 0:
        return 0
    ratio = overlap / float(denominator)
    if ratio >= 0.75:
        return 30
    if ratio >= 0.5:
        return 15
    return 0


def _source_family_score(media: ReleaseInfo, subtitle: ReleaseInfo, reasons: List[str]) -> int:
    if not media.source_family or not subtitle.source_family:
        return 0
    if media.source_family == subtitle.source_family:
        reasons.append("same_source_family:%s" % media.source_family)
        return 30
    if {media.source_family, subtitle.source_family} == {"web", "bluray"}:
        reasons.append("source_family_mismatch:%s->%s" % (media.source_family, subtitle.source_family))
        return -80
    reasons.append("source_family_mismatch:%s->%s" % (media.source_family, subtitle.source_family))
    return -40


def _edition_score(media: ReleaseInfo, subtitle: ReleaseInfo, reasons: List[str]) -> int:
    if media.editions == subtitle.editions:
        score = 0
        for edition in sorted(media.editions):
            reasons.append("same_edition:%s" % edition)
            score += 10
        return score
    mismatches = sorted(media.editions ^ subtitle.editions)
    for edition in mismatches:
        reasons.append("edition_mismatch:%s" % edition)
    return -100


def _first_match(tokens: Sequence[str], pattern: re.Pattern[str]) -> Optional[str]:
    for token in tokens:
        if pattern.fullmatch(token):
            return token
    return None
