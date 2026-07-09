from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import (
    DownloadedSubtitle,
    HttpDownload,
    HttpGet,
    HttpPost,
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
    post_json,
    subtitle_download_headers,
    trim_results,
    write_downloaded_subtitle,
)


DEFAULT_USER_AGENT = "MediaSubtitleTranslator v0.1.0"


@dataclass(frozen=True)
class OpenSubtitlesLoginResult:
    token: str
    base_url: str


@dataclass(frozen=True)
class OpenSubtitlesConfig:
    api_key: str
    user_agent: str = DEFAULT_USER_AGENT
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    base_url: str = "https://api.opensubtitles.com/api/v1"
    timeout: float = 20.0

    @classmethod
    def from_values(
        cls,
        api_key: Optional[str],
        user_agent: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 20.0,
    ) -> "OpenSubtitlesConfig":
        if not api_key:
            raise SubtitleProviderConfigurationError(
                "OPENSUBTITLES_API_KEY is required; set OPENSUBTITLES_API_KEY or pass --opensubtitles-api-key"
            )
        if not token:
            if not username:
                raise SubtitleProviderConfigurationError(
                    "OPENSUBTITLES_USERNAME is required; set OPENSUBTITLES_USERNAME or pass --opensubtitles-username"
                )
            if not password:
                raise SubtitleProviderConfigurationError(
                    "OPENSUBTITLES_PASSWORD is required; set OPENSUBTITLES_PASSWORD or pass --opensubtitles-password"
                )
        return cls(
            api_key=api_key,
            user_agent=user_agent or DEFAULT_USER_AGENT,
            username=username,
            password=password,
            token=token,
            base_url=(base_url or "https://api.opensubtitles.com/api/v1").rstrip("/"),
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> "OpenSubtitlesConfig":
        return cls.from_values(
            api_key=os.environ.get("OPENSUBTITLES_API_KEY"),
            user_agent=os.environ.get("OPENSUBTITLES_USER_AGENT"),
            username=os.environ.get("OPENSUBTITLES_USERNAME"),
            password=os.environ.get("OPENSUBTITLES_PASSWORD"),
            token=os.environ.get("OPENSUBTITLES_TOKEN"),
            base_url=os.environ.get("OPENSUBTITLES_BASE_URL"),
        )


class OpenSubtitlesProvider:
    name = "opensubtitles"

    def __init__(
        self,
        config: OpenSubtitlesConfig,
        http_get: HttpGet = get_json,
        http_post: HttpPost = post_json,
        http_download: HttpDownload = download_bytes,
    ):
        self.config = config
        self.http_get = http_get
        self.http_post = http_post
        self.http_download = http_download

    def login(self) -> OpenSubtitlesLoginResult:
        if self.config.token:
            return OpenSubtitlesLoginResult(token=self.config.token, base_url=self.config.base_url)
        if not self.config.username or not self.config.password:
            raise SubtitleProviderConfigurationError("OpenSubtitles login requires OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD")
        payload = self.http_post(
            self.config.base_url + "/login",
            {"username": self.config.username, "password": self.config.password},
            _opensubtitles_headers(self.config, content_type="application/json"),
            self.config.timeout,
        )
        token = payload.get("token")
        if not token:
            raise SubtitleProviderApiError("OpenSubtitles login response did not include token")
        return OpenSubtitlesLoginResult(token=str(token), base_url=_login_base_url(payload.get("base_url"), self.config.base_url))

    def search(self, request: SubtitleSearchRequest) -> List[SubtitleCandidate]:
        ensure_search_request_has_criteria(request)
        params = compact_params(
            {
                "imdb_id": _opensubtitles_imdb_id(request.imdb_id),
                "tmdb_id": request.tmdb_id,
                "query": request.file_name or request.title,
                "year": str(request.year) if request.year is not None else None,
                "season_number": str(request.season) if request.season is not None else None,
                "episode_number": str(request.episode) if request.episode is not None else None,
                "type": _opensubtitles_media_type(request.media_type),
                "languages": _opensubtitles_languages(request.languages),
                "page": "1",
            }
        )
        headers = _opensubtitles_headers(self.config)
        if self.config.token:
            headers["Authorization"] = "Bearer %s" % self.config.token
        payload = self.http_get(
            self.config.base_url + "/subtitles",
            params,
            headers,
            self.config.timeout,
        )
        return trim_results(_parse_candidates(payload), request.limit)

    def download(
        self,
        candidate: SubtitleCandidate,
        output_dir: Path,
        force: bool = False,
        target_season: Optional[int] = None,
        target_episode: Optional[int] = None,
    ) -> DownloadedSubtitle:
        file_id = _candidate_file_id(candidate)
        login_result = self.login()
        headers = _opensubtitles_headers(self.config, content_type="application/json")
        headers["Authorization"] = "Bearer %s" % login_result.token
        payload = self.http_post(
            login_result.base_url.rstrip("/") + "/download",
            {"file_id": int(file_id)},
            headers,
            self.config.timeout,
        )
        link = payload.get("link")
        if not link:
            raise SubtitleProviderApiError("OpenSubtitles download response did not include link")
        file_name = str(payload.get("file_name") or candidate.file_name or "opensubtitles-%s.srt" % file_id)
        downloaded = self.http_download(str(link), subtitle_download_headers(self.config.user_agent), self.config.timeout)
        return write_downloaded_subtitle(
            provider="opensubtitles",
            payload=downloaded,
            output_dir=output_dir,
            suggested_name=file_name,
            source_url=str(link),
            force=force,
            target_season=target_season,
            target_episode=target_episode,
        )


def _opensubtitles_imdb_id(imdb_id: Optional[str]) -> Optional[str]:
    if not imdb_id:
        return None
    cleaned = imdb_id.strip()
    if cleaned.lower().startswith("tt"):
        return cleaned[2:]
    return cleaned


def _opensubtitles_media_type(media_type: Optional[str]) -> Optional[str]:
    if not media_type:
        return None
    value = media_type.strip().lower()
    if value in {"episode", "tv"}:
        return "episode"
    if value == "movie":
        return "movie"
    return value


def _opensubtitles_languages(languages) -> Optional[str]:
    codes = [_opensubtitles_language_code(code) for code in normalized_language_codes(tuple(languages))]
    return ",".join(codes) if codes else None


def _opensubtitles_language_code(language_code: str) -> str:
    if language_code == "zh":
        return "zh-cn"
    return language_code


def _candidate_file_id(candidate: SubtitleCandidate) -> str:
    value = candidate.file_id or (candidate.download or {}).get("file_id")
    if value is None:
        raise SubtitleProviderApiError("OpenSubtitles download requires file_id")
    text = str(value)
    if not text.isdigit():
        raise SubtitleProviderApiError("OpenSubtitles file_id must be numeric")
    return text


def _opensubtitles_headers(config: OpenSubtitlesConfig, content_type: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Api-Key": config.api_key,
        "User-Agent": config.user_agent,
        "X-User-Agent": config.user_agent,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _login_base_url(base_url: Any, fallback: str) -> str:
    if not base_url:
        return fallback
    value = str(base_url).strip().rstrip("/")
    if not value:
        return fallback
    if value.startswith("http://") or value.startswith("https://"):
        return value if value.endswith("/api/v1") else value + "/api/v1"
    return "https://" + value + "/api/v1"


def _parse_candidates(payload: Dict[str, Any]) -> List[SubtitleCandidate]:
    results: List[SubtitleCandidate] = []
    for item in payload.get("data") or []:
        attributes = item.get("attributes") or {}
        files = attributes.get("files") or [{}]
        for file_data in files:
            file_id = file_data.get("file_id")
            file_id_text = str(file_id) if file_id is not None else None
            provider_id = str(item.get("id") or file_id_text or "")
            if not provider_id:
                continue
            results.append(
                SubtitleCandidate(
                    provider="opensubtitles",
                    provider_id=provider_id,
                    language=normalize_candidate_language(attributes.get("language")),
                    release_name=attributes.get("release"),
                    file_name=file_data.get("file_name"),
                    file_id=file_id_text,
                    subtitle_format=_format_from_filename(file_data.get("file_name")),
                    hearing_impaired=_optional_bool(attributes.get("hearing_impaired")),
                    score=_optional_float(attributes.get("ratings")),
                    download={
                        "method": "opensubtitles-download",
                        "file_id": file_id_text,
                        "requires_token": True,
                    }
                    if file_id_text
                    else {"method": "opensubtitles-download", "requires_token": True},
                    metadata={
                        "download_count": attributes.get("download_count"),
                        "feature_details": attributes.get("feature_details"),
                    },
                )
            )
    return results


def _format_from_filename(file_name: Optional[str]) -> Optional[str]:
    if not file_name or "." not in file_name:
        return None
    return file_name.rsplit(".", 1)[-1].lower()


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
