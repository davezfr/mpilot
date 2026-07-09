from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .languages import language_to_code, normalize_language_label
from .plex_resolver import PlexResolvedMedia
from .provider_policy import DEFAULT_DOWNLOAD_PROVIDER_PRIORITY, DEFAULT_SEARCH_PROVIDERS
from .source import SubtitleStream, probe_subtitle_streams


ProbeRunner = Callable[[Path], List[SubtitleStream]]


@dataclass(frozen=True)
class LocalSubtitleCandidate:
    kind: str
    language: Optional[str]
    path: Optional[Path] = None
    stream_index: Optional[int] = None
    codec: Optional[str] = None
    title: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "kind": self.kind,
            "language": self.language,
        }
        if self.path is not None:
            data["path"] = str(self.path)
        if self.stream_index is not None:
            data["stream_index"] = self.stream_index
        if self.codec is not None:
            data["codec"] = self.codec
        if self.title is not None:
            data["title"] = self.title
        return data


def build_subtitle_plan(
    resolved_media: PlexResolvedMedia,
    target_language: str,
    preferred_source_language: str = "en",
    probe_runner: Optional[ProbeRunner] = None,
) -> Dict[str, Any]:
    video_path = Path(resolved_media.local_file)
    if not video_path.exists():
        raise FileNotFoundError("resolved Plex local_file does not exist: %s" % video_path)

    target_code = language_to_code(target_language)
    preferred_source_code = language_to_code(preferred_source_language)
    events = [{"event": "local_scan_started", "target_language": target_code}]

    sidecars = scan_sidecar_subtitles(video_path)
    streams = probe_runner(video_path) if probe_runner else probe_subtitle_streams(video_path)
    embedded_text = [
        candidate
        for candidate in (_candidate_from_stream(stream, "embedded-text") for stream in streams if stream.is_text)
        if candidate.language is not None
    ]
    embedded_image = [
        candidate
        for candidate in (_candidate_from_stream(stream, "embedded-image") for stream in streams if stream.is_image)
        if candidate.language is not None
    ]
    text_sources = sidecars + embedded_text

    target_source = _first_language_match(text_sources, target_code)
    if target_source is not None:
        events.append({"event": "target_subtitle_found", "language": target_code, "source": target_source.to_dict()})
        proposal = {
            "action": "use_existing",
            "target_language": target_code,
            "source": target_source.to_dict(),
        }
        return _plan_result(resolved_media, target_code, preferred_source_code, events, sidecars, embedded_text, embedded_image, proposal)

    translation_source = _pick_translation_source(text_sources, target_code, preferred_source_code)
    if translation_source is not None:
        source_language = translation_source.language
        events.append({"event": "translation_source_found", "language": source_language, "source": translation_source.to_dict()})
        proposal: Dict[str, Any] = {
            "action": "translate",
            "source_language": source_language,
            "target_language": target_code,
            "output_modes": ["single-srt", "bilingual-ass"],
            "source": translation_source.to_dict(),
        }
        if source_language != preferred_source_code:
            proposal["confirmation_needed"] = True
            proposal["confirmation_reason"] = "source_language_mismatch"
            proposal["preferred_source_language"] = preferred_source_code
        return _plan_result(resolved_media, target_code, preferred_source_code, events, sidecars, embedded_text, embedded_image, proposal)

    reason = "no_local_text_subtitles"
    if embedded_image:
        reason = "only_image_subtitles_found"
        events.append({"event": "image_subtitles_found", "count": len(embedded_image)})
    events.append({"event": "online_search_needed", "reason": reason})
    proposal = {
        "action": "online_search",
        "target_language": target_code,
        "reason": reason,
        "providers": list(DEFAULT_SEARCH_PROVIDERS),
        "download_provider_priority": list(DEFAULT_DOWNLOAD_PROVIDER_PRIORITY),
    }
    return _plan_result(resolved_media, target_code, preferred_source_code, events, sidecars, embedded_text, embedded_image, proposal)


def scan_sidecar_subtitles(video_path: Path) -> List[LocalSubtitleCandidate]:
    if not video_path.parent.exists():
        return []
    candidates = []
    for child in video_path.parent.iterdir():
        if child.suffix.lower() != ".srt":
            continue
        language = infer_sidecar_language(child, video_path.stem)
        if language is None:
            continue
        candidates.append(LocalSubtitleCandidate(kind="sidecar", language=language, path=child, codec="subrip"))
    return sorted(candidates, key=lambda candidate: str(candidate.path or "").lower())


def infer_sidecar_language(path: Path, media_stem: str) -> Optional[str]:
    stem = path.stem
    lower_stem = stem.lower()
    lower_media = media_stem.lower()
    suffix = None
    for separator in (".", "_"):
        prefix = lower_media + separator
        if lower_stem.startswith(prefix):
            suffix = stem[len(media_stem) + len(separator) :]
            break
    if not suffix:
        return None
    guesses = [suffix]
    guesses.extend(token for token in re.split(r"[._ -]+", suffix) if token)
    for guess in guesses:
        try:
            return language_to_code(guess)
        except ValueError:
            continue
    return None


def _candidate_from_stream(stream: SubtitleStream, kind: str) -> LocalSubtitleCandidate:
    return LocalSubtitleCandidate(
        kind=kind,
        language=_stream_language_code(stream),
        stream_index=stream.index,
        codec=stream.codec_name,
        title=stream.title,
    )


def _stream_language_code(stream: SubtitleStream) -> Optional[str]:
    for value in (stream.language, stream.title):
        if not value:
            continue
        try:
            return language_to_code(normalize_language_label(value))
        except ValueError:
            continue
    return None


def _first_language_match(candidates: List[LocalSubtitleCandidate], language_code: str) -> Optional[LocalSubtitleCandidate]:
    for candidate in candidates:
        if candidate.language == language_code:
            return candidate
    return None


def _pick_translation_source(
    candidates: List[LocalSubtitleCandidate],
    target_code: str,
    preferred_source_code: str,
) -> Optional[LocalSubtitleCandidate]:
    usable = [candidate for candidate in candidates if candidate.language and candidate.language != target_code]
    preferred = _first_language_match(usable, preferred_source_code)
    if preferred is not None:
        return preferred
    return usable[0] if usable else None


def _plan_result(
    resolved_media: PlexResolvedMedia,
    target_code: str,
    preferred_source_code: str,
    events: List[Dict[str, Any]],
    sidecars: List[LocalSubtitleCandidate],
    embedded_text: List[LocalSubtitleCandidate],
    embedded_image: List[LocalSubtitleCandidate],
    proposal: Dict[str, Any],
) -> Dict[str, Any]:
    text_sources = sidecars + embedded_text
    available_source_languages = sorted(
        {c.language for c in text_sources if c.language and c.language != target_code}
    )
    return {
        "plex": resolved_media.to_dict(),
        "input": resolved_media.local_file,
        "target_language": target_code,
        "preferred_source_language": preferred_source_code,
        "available_source_languages": available_source_languages,
        "events": events,
        "local_sources": {
            "sidecars": [candidate.to_dict() for candidate in sidecars],
            "embedded_text": [candidate.to_dict() for candidate in embedded_text],
            "embedded_image": [candidate.to_dict() for candidate in embedded_image],
        },
        "proposal": proposal,
    }
