from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import (
    DownloadedSubtitle,
    HttpDownload,
    HttpGet,
    SubtitleCandidate,
    SubtitleProviderApiError,
    SubtitleProviderConfigurationError,
    SubtitleSearchRequest,
    compact_params,
    download_bytes,
    ensure_search_request_has_criteria,
    get_json,
    normalize_candidate_language,
    normalized_language_codes,
    subtitle_download_headers,
    trim_results,
    write_downloaded_subtitle,
)


@dataclass(frozen=True)
class SubDLConfig:
    api_key: str
    base_url: str = "https://api.subdl.com/api/v1"
    download_base_url: str = "https://dl.subdl.com"
    timeout: float = 20.0

    @classmethod
    def from_values(
        cls,
        api_key: Optional[str],
        base_url: Optional[str] = None,
        download_base_url: Optional[str] = None,
        timeout: float = 20.0,
    ) -> "SubDLConfig":
        if not api_key:
            raise SubtitleProviderConfigurationError("SUBDL_API_KEY is required; set SUBDL_API_KEY or pass --subdl-api-key")
        return cls(
            api_key=api_key,
            base_url=(base_url or "https://api.subdl.com/api/v1").rstrip("/"),
            download_base_url=(download_base_url or "https://dl.subdl.com").rstrip("/"),
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> "SubDLConfig":
        return cls.from_values(
            api_key=os.environ.get("SUBDL_API_KEY"),
            base_url=os.environ.get("SUBDL_BASE_URL"),
            download_base_url=os.environ.get("SUBDL_DOWNLOAD_BASE_URL"),
        )


class SubDLProvider:
    name = "subdl"

    def __init__(self, config: SubDLConfig, http_get: HttpGet = get_json, http_download: HttpDownload = download_bytes):
        self.config = config
        self.http_get = http_get
        self.http_download = http_download

    def search(self, request: SubtitleSearchRequest) -> List[SubtitleCandidate]:
        ensure_search_request_has_criteria(request)
        params = compact_params(
            {
                "api_key": self.config.api_key,
                "imdb_id": request.imdb_id,
                "tmdb_id": request.tmdb_id,
                "film_name": _subdl_film_name(request),
                "year": str(request.year) if request.year is not None else None,
                "season_number": str(request.season) if request.season is not None else None,
                "episode_number": str(request.episode) if request.episode is not None else None,
                "type": _subdl_media_type(request.media_type),
                "languages": _subdl_languages(request.languages),
                "subs_per_page": str(request.limit),
                "unpack": "1",
            }
        )
        payload = self.http_get(
            self.config.base_url + "/subtitles",
            params,
            {"Accept": "application/json"},
            self.config.timeout,
        )
        if payload.get("status") is False:
            raise SubtitleProviderApiError("SubDL search failed: %s" % (payload.get("message") or payload.get("error") or payload))
        return trim_results(_parse_candidates(payload, self.config.download_base_url), request.limit)

    def download(
        self,
        candidate: SubtitleCandidate,
        output_dir: Path,
        force: bool = False,
        target_season: Optional[int] = None,
        target_episode: Optional[int] = None,
    ) -> DownloadedSubtitle:
        download = candidate.download or {}
        url = download.get("url")
        if not url:
            raise SubtitleProviderApiError("SubDL download requires a direct URL from candidate.download.url")
        payload = self.http_download(str(url), subtitle_download_headers(), self.config.timeout)
        metadata = candidate.metadata or {}
        effective_season = target_season if target_season is not None else _optional_int(metadata.get("season"))
        effective_episode = target_episode if target_episode is not None else _optional_int(metadata.get("episode"))
        return write_downloaded_subtitle(
            provider="subdl",
            payload=payload,
            output_dir=output_dir,
            suggested_name=candidate.file_name,
            source_url=str(url),
            force=force,
            target_season=effective_season,
            target_episode=effective_episode,
        )


def _subdl_media_type(media_type: Optional[str]) -> Optional[str]:
    if not media_type:
        return None
    value = media_type.strip().lower()
    if value in {"episode", "tv"}:
        return "tv"
    if value == "movie":
        return "movie"
    return value


def _subdl_languages(languages) -> Optional[str]:
    codes = [_subdl_language_code(code) for code in normalized_language_codes(tuple(languages))]
    return ",".join(codes) if codes else None


def _subdl_film_name(request: SubtitleSearchRequest) -> Optional[str]:
    if request.title:
        return request.title
    if not request.file_name:
        return None
    stem = Path(request.file_name).stem
    safe = "".join(ch if (ch.isalnum() or ch in {" ", ".", "-", "_"}) else " " for ch in stem)
    safe = safe.replace(".", " ").replace("_", " ")
    return " ".join(safe.split()) or None


def _subdl_language_code(language_code: str) -> str:
    return language_code.upper()


def _parse_candidates(payload: Dict[str, Any], download_base_url: str) -> List[SubtitleCandidate]:
    results: List[SubtitleCandidate] = []
    for item in payload.get("subtitles") or []:
        unpacked = item.get("unpack_files") or []
        if unpacked:
            results.extend(_candidate_from_subdl_file(file_data, item, download_base_url) for file_data in unpacked)
        else:
            results.append(_candidate_from_subdl_file(item, item, download_base_url))
    return [candidate for candidate in results if candidate.provider_id]


def _candidate_from_subdl_file(file_data: Dict[str, Any], item: Dict[str, Any], download_base_url: str) -> SubtitleCandidate:
    relative_url = file_data.get("url") or item.get("url") or ""
    download_url = _absolute_download_url(relative_url, download_base_url) if relative_url else None
    return SubtitleCandidate(
        provider="subdl",
        provider_id=str(relative_url or file_data.get("id") or item.get("id") or ""),
        language=normalize_candidate_language(file_data.get("language") or item.get("language")),
        release_name=file_data.get("release_name") or item.get("release_name"),
        file_name=file_data.get("name") or file_data.get("file_name") or item.get("name") or item.get("file_name"),
        file_id=str(file_data.get("id") or item.get("id")) if (file_data.get("id") or item.get("id")) else None,
        subtitle_format=file_data.get("format") or item.get("format") or _format_from_url(relative_url),
        hearing_impaired=_optional_bool(file_data.get("hi") if "hi" in file_data else item.get("hi")),
        download={"method": "direct-url", "url": download_url} if download_url else None,
        metadata={
            "season": file_data.get("season") or item.get("season"),
            "episode": file_data.get("episode") or item.get("episode"),
        },
    )


def _absolute_download_url(relative_url: str, download_base_url: str) -> str:
    if relative_url.startswith("http://") or relative_url.startswith("https://"):
        return relative_url
    if relative_url.startswith("/"):
        return download_base_url + relative_url
    return download_base_url + "/" + relative_url


def _format_from_url(url: str) -> Optional[str]:
    if not url or "." not in url:
        return None
    return url.rsplit(".", 1)[-1].lower()


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
