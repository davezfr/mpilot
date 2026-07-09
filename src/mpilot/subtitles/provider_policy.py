from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .providers.base import DownloadedSubtitle, SubtitleCandidate, SubtitleProviderApiError, SubtitleProviderError
from .subtitle_matching import SubtitleMatchScore, confidence_for_score, parse_release_info, rank_subtitle_candidates


KNOWN_SUBTITLE_PROVIDERS = ("subdl", "opensubtitles")
DEFAULT_SEARCH_PROVIDERS = KNOWN_SUBTITLE_PROVIDERS
DEFAULT_DOWNLOAD_PROVIDER_PRIORITY = ("subdl", "opensubtitles")
DEFAULT_MINIMUM_RELEASE_MATCH_CONFIDENCE = "medium"
DOWNLOAD_FORMAT_PREFERENCE = {
    ".srt": 0,
    ".ass": 1,
    ".ssa": 2,
    ".vtt": 3,
    ".sub": 4,
}
LOW_CONFIDENCE_SUBTITLE_MESSAGE_KEY = "low_confidence_subtitle_confirmation"
LOW_CONFIDENCE_SUBTITLE_MESSAGE = (
    "We found subtitle candidates, but they may not match the video timeline perfectly. Continue anyway?"
)


@dataclass(frozen=True)
class ProviderDownloadSelection:
    candidate: SubtitleCandidate
    download: DownloadedSubtitle
    attempts: List[Dict[str, Any]]
    match: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "candidate": self.candidate.to_dict(),
            "download": self.download.to_dict(),
            "attempts": self.attempts,
        }
        if self.match:
            data["match"] = self.match
        return data


class LowConfidenceSubtitleCandidatesError(SubtitleProviderApiError):
    def __init__(self, attempts: List[Dict[str, Any]], candidates: List[RankedDownloadCandidate], minimum_confidence: str):
        super().__init__("low-confidence subtitle candidates require confirmation")
        self.attempts = attempts
        self.candidates = candidates
        self.minimum_confidence = minimum_confidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "confirm_low_confidence_subtitle",
            "confirmation_reason": "low_confidence_match",
            "message_key": LOW_CONFIDENCE_SUBTITLE_MESSAGE_KEY,
            "message": LOW_CONFIDENCE_SUBTITLE_MESSAGE,
            "minimum_release_match_confidence": self.minimum_confidence,
            "attempts": self.attempts,
            "candidates": [_ranked_candidate_to_dict(candidate) for candidate in self.candidates],
        }


PROVIDER_FALLBACK_LANGUAGE_MESSAGE_KEY = "provider_fallback_language_confirmation"


class ProviderFallbackLanguageError(SubtitleProviderApiError):
    def __init__(
        self,
        selection: "ProviderDownloadSelection",
        requested_language: str,
        found_language: str,
        search_stage: int,
    ):
        super().__init__(
            "provider subtitle language %r does not match requested %r" % (found_language, requested_language)
        )
        self.selection = selection
        self.requested_language = requested_language
        self.found_language = found_language
        self.search_stage = search_stage

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "confirm_provider_fallback_language",
            "confirmation_reason": "provider_fallback_language",
            "message_key": PROVIDER_FALLBACK_LANGUAGE_MESSAGE_KEY,
            "message": (
                "No %r subtitle was found. The best available is %r. Translate from %r instead?"
                % (self.requested_language, self.found_language, self.found_language)
            ),
            "requested_language": self.requested_language,
            "found_language": self.found_language,
            "search_stage": self.search_stage,
            "selection": self.selection.to_dict(),
        }


@dataclass(frozen=True)
class RankedDownloadCandidate:
    candidate: SubtitleCandidate
    match: Optional[SubtitleMatchScore] = None


def provider_names_for_search(provider_name: str) -> List[str]:
    if provider_name == "all":
        return list(DEFAULT_SEARCH_PROVIDERS)
    ensure_known_provider(provider_name)
    return [provider_name]


def parse_provider_priority(value: str) -> List[str]:
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    if not names:
        return list(DEFAULT_DOWNLOAD_PROVIDER_PRIORITY)
    for name in names:
        ensure_known_provider(name)
    return names


def rank_candidates_for_download(
    candidates: Iterable[SubtitleCandidate],
    provider_priority: Sequence[str] = DEFAULT_DOWNLOAD_PROVIDER_PRIORITY,
    media_release_name: Optional[str] = None,
) -> List[SubtitleCandidate]:
    return [entry.candidate for entry in _rank_download_candidates(candidates, provider_priority, media_release_name)]


def _rank_download_candidates(
    candidates: Iterable[SubtitleCandidate],
    provider_priority: Sequence[str] = DEFAULT_DOWNLOAD_PROVIDER_PRIORITY,
    media_release_name: Optional[str] = None,
) -> List[RankedDownloadCandidate]:
    candidate_list = list(candidates)
    priority = {provider: index for index, provider in enumerate(provider_priority)}
    fallback_index = len(priority)
    if media_release_name:
        media = parse_release_info(media_release_name)
        return [
            RankedDownloadCandidate(candidate=scored.candidate, match=scored)
            for scored in sorted(
                rank_subtitle_candidates(media, candidate_list, provider_priority),
                key=lambda item: (
                    -item.score,
                    _download_format_rank(item.candidate),
                    priority.get(item.candidate.provider, fallback_index),
                    item.candidate.provider,
                ),
            )
        ]
    return [
        RankedDownloadCandidate(candidate=candidate)
        for candidate in sorted(
            candidate_list,
            key=lambda candidate: (
                priority.get(candidate.provider, fallback_index),
                candidate.provider,
                _download_format_rank(candidate),
            ),
        )
    ]


def download_first_provider_candidate(
    candidates: Iterable[SubtitleCandidate],
    providers: Mapping[str, Any],
    output_dir: Path,
    *,
    force: bool = False,
    provider_priority: Sequence[str] = DEFAULT_DOWNLOAD_PROVIDER_PRIORITY,
    media_release_name: Optional[str] = None,
    target_season: Optional[int] = None,
    target_episode: Optional[int] = None,
    minimum_release_match_confidence: str = DEFAULT_MINIMUM_RELEASE_MATCH_CONFIDENCE,
    allow_low_confidence: bool = False,
) -> ProviderDownloadSelection:
    attempts: List[Dict[str, Any]] = []
    low_confidence_candidates: List[RankedDownloadCandidate] = []
    media = parse_release_info(media_release_name) if media_release_name else None
    requested_season = target_season if target_season is not None else (media.season if media else None)
    requested_episode = target_episode if target_episode is not None else (media.episode if media else None)
    ranked_candidates = _rank_download_candidates(candidates, provider_priority, media_release_name)
    if not ranked_candidates:
        raise SubtitleProviderApiError("no subtitle candidates are available to download")
    for entry in ranked_candidates:
        candidate = entry.candidate
        match = _match_to_dict(entry.match)
        episode_mismatch = _episode_mismatch_reason(requested_season, requested_episode, candidate)
        if episode_mismatch:
            attempts.append(_attempt(candidate, "skipped", episode_mismatch, match=match))
            continue
        source_family_incompatibility = _source_family_incompatibility_reason(entry.match)
        if source_family_incompatibility:
            attempts.append(_attempt(candidate, "skipped", source_family_incompatibility, match=match))
            continue
        if (
            entry.match
            and not allow_low_confidence
            and _confidence_rank(entry.match.confidence) < _confidence_rank(minimum_release_match_confidence)
        ):
            low_confidence_candidates.append(entry)
            attempts.append(
                _attempt(
                    candidate,
                    "skipped",
                    "release match confidence %s below %s" % (entry.match.confidence, minimum_release_match_confidence),
                    match=match,
                )
            )
            continue
        provider = providers.get(candidate.provider)
        if provider is None:
            attempts.append(_attempt(candidate, "skipped", "provider is not configured", match=match))
            continue
        try:
            downloaded = provider.download(
                candidate,
                output_dir,
                force=force,
                target_season=requested_season,
                target_episode=requested_episode,
            )
        except SubtitleProviderError as error:
            attempts.append(_attempt(candidate, "error", str(error), match=match))
            continue
        attempts.append(_attempt(candidate, "ok", match=match))
        return ProviderDownloadSelection(candidate=candidate, download=downloaded, attempts=attempts, match=match)
    if low_confidence_candidates:
        raise LowConfidenceSubtitleCandidatesError(attempts, low_confidence_candidates, minimum_release_match_confidence)
    raise SubtitleProviderApiError("no subtitle candidates could be downloaded")


def ensure_known_provider(provider_name: str) -> None:
    if provider_name not in KNOWN_SUBTITLE_PROVIDERS:
        raise ValueError("unknown subtitle provider: %s" % provider_name)


def _confidence_rank(confidence: str) -> int:
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("unknown release match confidence: %s" % confidence)
    return {"low": 0, "medium": 1, "high": 2}[confidence]


def _match_to_dict(match: Optional[SubtitleMatchScore]) -> Optional[Dict[str, Any]]:
    if not match:
        return None
    return {
        "score": match.score,
        "confidence": confidence_for_score(match.score),
        "reasons": list(match.reasons),
    }


def _ranked_candidate_to_dict(entry: RankedDownloadCandidate) -> Dict[str, Any]:
    data = entry.candidate.to_dict()
    match = _match_to_dict(entry.match)
    if match:
        data["match"] = match
    return data


def _episode_mismatch_reason(
    requested_season: Optional[int],
    requested_episode: Optional[int],
    candidate: SubtitleCandidate,
) -> str:
    if requested_season is None or requested_episode is None:
        return ""
    candidate_season, candidate_episode = _candidate_season_episode(candidate)
    if candidate_season is None or candidate_episode is None:
        return ""
    if candidate_season == requested_season and candidate_episode == requested_episode:
        return ""
    return "episode mismatch: media S%02dE%02d candidate S%02dE%02d" % (
        requested_season,
        requested_episode,
        candidate_season,
        candidate_episode,
    )


def _source_family_incompatibility_reason(match: Optional[SubtitleMatchScore]) -> str:
    if not match:
        return ""
    for reason in match.reasons:
        if not reason.startswith("source_family_mismatch:"):
            continue
        families = reason.split(":", 1)[1].split("->", 1)
        if len(families) == 2 and set(families) == {"web", "bluray"}:
            return "incompatible source family: media %s candidate %s" % (families[0], families[1])
    return ""


def _candidate_season_episode(candidate: SubtitleCandidate):
    metadata = candidate.metadata or {}
    metadata_season = _optional_int(metadata.get("season"))
    metadata_episode = _optional_int(metadata.get("episode"))
    if metadata_season is not None and metadata_episode is not None:
        return metadata_season, metadata_episode
    release_text = candidate.release_name or candidate.file_name or candidate.provider_id
    parsed = parse_release_info(release_text)
    return parsed.season, parsed.episode


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _attempt(candidate: SubtitleCandidate, status: str, reason: str = "", match: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = {
        "provider": candidate.provider,
        "provider_id": candidate.provider_id,
        "status": status,
    }
    if reason:
        data["reason"] = reason
    if match:
        data["match"] = match
    return data


def _download_format_rank(candidate: SubtitleCandidate) -> int:
    for value in (
        candidate.subtitle_format,
        candidate.file_name,
        candidate.provider_id,
        (candidate.download or {}).get("url"),
    ):
        suffix = _subtitle_suffix(value)
        if suffix in DOWNLOAD_FORMAT_PREFERENCE:
            return DOWNLOAD_FORMAT_PREFERENCE[suffix]
    return 99


def _subtitle_suffix(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().split("?", 1)[0].split("#", 1)[0]
    if not text:
        return ""
    if "." not in text:
        return "." + text if text in {"srt", "ass", "ssa", "vtt", "sub"} else ""
    return Path(text).suffix.lower()
