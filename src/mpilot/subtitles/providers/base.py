from __future__ import annotations

import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..languages import language_to_code


class SubtitleProviderError(RuntimeError):
    """Base class for subtitle provider failures."""


class SubtitleProviderConfigurationError(SubtitleProviderError):
    """Raised when a selected provider is missing required settings."""


class SubtitleProviderApiError(SubtitleProviderError):
    """Raised when a provider returns an HTTP or JSON error."""


HttpGet = Callable[[str, Dict[str, str], Dict[str, str], float], Dict[str, Any]]
HttpPost = Callable[[str, Dict[str, Any], Dict[str, str], float], Dict[str, Any]]
HttpDownload = Callable[[str, Dict[str, str], float], bytes]
DEFAULT_DOWNLOAD_USER_AGENT = "MediaSubtitleTranslator v0.1.0"
SUPPORTED_DOWNLOADED_SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
MAX_PROVIDER_JSON_BYTES = 5 * 1024 * 1024
MAX_PROVIDER_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 1_000
MAX_EXTRACTED_SUBTITLE_BYTES = 20 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200
MAX_HTTP_ERROR_BYTES = 64 * 1024
DOWNLOADED_SUBTITLE_SUFFIX_PREFERENCE = {
    ".srt": 0,
    ".ass": 1,
    ".ssa": 2,
    ".vtt": 3,
    ".sub": 4,
}


@dataclass(frozen=True)
class SubtitleSearchRequest:
    media_type: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    file_name: Optional[str] = None
    languages: Tuple[str, ...] = ()
    limit: int = 10

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "languages": list(self.languages),
            "limit": self.limit,
        }
        if self.media_type:
            data["media_type"] = self.media_type
        if self.title:
            data["title"] = self.title
        if self.year is not None:
            data["year"] = self.year
        if self.imdb_id:
            data["imdb_id"] = self.imdb_id
        if self.tmdb_id:
            data["tmdb_id"] = self.tmdb_id
        if self.season is not None:
            data["season"] = self.season
        if self.episode is not None:
            data["episode"] = self.episode
        if self.file_name:
            data["file_name"] = self.file_name
        return data


@dataclass(frozen=True)
class SubtitleCandidate:
    provider: str
    provider_id: str
    language: Optional[str]
    release_name: Optional[str] = None
    file_name: Optional[str] = None
    file_id: Optional[str] = None
    subtitle_format: Optional[str] = None
    hearing_impaired: Optional[bool] = None
    score: Optional[float] = None
    download: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "language": self.language,
        }
        if self.release_name:
            data["release_name"] = self.release_name
        if self.file_name:
            data["file_name"] = self.file_name
        if self.file_id:
            data["file_id"] = self.file_id
        if self.subtitle_format:
            data["format"] = self.subtitle_format
        if self.hearing_impaired is not None:
            data["hearing_impaired"] = self.hearing_impaired
        if self.score is not None:
            data["score"] = self.score
        if self.download:
            data["download"] = self.download
        if self.metadata:
            data["metadata"] = self.metadata
        return data


@dataclass(frozen=True)
class DownloadedSubtitle:
    provider: str
    path: Path
    source_url: Optional[str] = None
    archive_path: Optional[Path] = None
    extracted_from_archive: bool = False
    size_bytes: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "provider": self.provider,
            "path": str(self.path),
            "extracted_from_archive": self.extracted_from_archive,
        }
        if self.source_url:
            data["source_url"] = self.source_url
        if self.archive_path:
            data["archive_path"] = str(self.archive_path)
        if self.size_bytes is not None:
            data["size_bytes"] = self.size_bytes
        return data


def get_json(url: str, params: Dict[str, str], headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    _require_http_url(url, "subtitle provider URL")
    query = urllib.parse.urlencode(params)
    full_url = url + ("?" + query if query else "")
    request = urllib.request.Request(full_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _read_limited(response, MAX_PROVIDER_JSON_BYTES, "subtitle provider JSON response").decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = compact_error_detail(error.read(MAX_HTTP_ERROR_BYTES + 1).decode("utf-8", errors="replace"))
        message = "subtitle provider request failed: HTTP %s" % error.code
        if detail:
            message += " %s" % detail
        raise SubtitleProviderApiError(message) from error
    except urllib.error.URLError as error:
        raise SubtitleProviderApiError("subtitle provider request failed: %s" % error.reason) from error
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SubtitleProviderApiError("subtitle provider returned non-JSON response") from error


def post_json(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    _require_http_url(url, "subtitle provider URL")
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = _read_limited(
                response, MAX_PROVIDER_JSON_BYTES, "subtitle provider JSON response"
            ).decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = compact_error_detail(error.read(MAX_HTTP_ERROR_BYTES + 1).decode("utf-8", errors="replace"))
        message = "subtitle provider request failed: HTTP %s" % error.code
        if detail:
            message += " %s" % detail
        raise SubtitleProviderApiError(message) from error
    except urllib.error.URLError as error:
        raise SubtitleProviderApiError("subtitle provider request failed: %s" % error.reason) from error
    try:
        return json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SubtitleProviderApiError("subtitle provider returned non-JSON response") from error


def download_bytes(url: str, headers: Dict[str, str], timeout: float) -> bytes:
    _require_http_url(url, "subtitle download URL")
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _read_limited(response, MAX_PROVIDER_DOWNLOAD_BYTES, "subtitle download")
    except urllib.error.HTTPError as error:
        detail = compact_error_detail(error.read(MAX_HTTP_ERROR_BYTES + 1).decode("utf-8", errors="replace"))
        message = "subtitle download failed: HTTP %s" % error.code
        if detail:
            message += " %s" % detail
        raise SubtitleProviderApiError(message) from error
    except urllib.error.URLError as error:
        raise SubtitleProviderApiError("subtitle download failed: %s" % error.reason) from error


def compact_params(params: Dict[str, Optional[str]]) -> Dict[str, str]:
    return {key: str(value) for key, value in params.items() if value not in (None, "")}


def _read_limited(stream: Any, limit: int, description: str) -> bytes:
    payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise SubtitleProviderApiError(f"{description} exceeded the {limit}-byte limit")
    return payload


def _require_http_url(url: str, description: str) -> None:
    if urllib.parse.urlparse(url).scheme.casefold() not in {"http", "https"}:
        raise SubtitleProviderApiError(f"{description} must use http or https")


def normalize_candidate_language(language: Any) -> Optional[str]:
    if language is None:
        return None
    value = str(language).strip()
    if not value:
        return None
    try:
        return language_to_code(value)
    except ValueError:
        return value.lower().replace("_", "-")


def normalized_language_codes(languages: Tuple[str, ...]) -> List[str]:
    return [language_to_code(language) for language in languages]


def ensure_search_request_has_criteria(request: SubtitleSearchRequest) -> None:
    if not any([request.imdb_id, request.tmdb_id, request.title, request.file_name]):
        raise ValueError("subtitle search requires at least one of --imdb, --tmdb, --title, or --file-name")


def trim_results(results: List[SubtitleCandidate], limit: int) -> List[SubtitleCandidate]:
    if limit <= 0:
        return []
    return results[:limit]


def compact_error_detail(detail: str, limit: int = 500) -> str:
    text = " ".join(detail.strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def subtitle_download_headers(user_agent: str = DEFAULT_DOWNLOAD_USER_AGENT) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "User-Agent": user_agent,
        "X-User-Agent": user_agent,
    }


def write_downloaded_subtitle(
    provider: str,
    payload: bytes,
    output_dir: Path,
    suggested_name: Optional[str],
    source_url: Optional[str],
    force: bool = False,
    target_season: Optional[int] = None,
    target_episode: Optional[int] = None,
) -> DownloadedSubtitle:
    if len(payload) > MAX_PROVIDER_DOWNLOAD_BYTES:
        raise SubtitleProviderApiError(
            f"subtitle download exceeded the {MAX_PROVIDER_DOWNLOAD_BYTES}-byte limit"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = sanitized_filename(suggested_name or url_filename(source_url) or "subtitle.srt")
    if is_zip_download(file_name, payload):
        archive_path = output_dir / archive_filename(file_name)
        ensure_can_write_download(archive_path, force)
        archive_path.write_bytes(payload)
        extracted_path = extract_first_subtitle_from_zip(
            archive_path,
            output_dir,
            force=force,
            target_season=target_season,
            target_episode=target_episode,
        )
        return DownloadedSubtitle(
            provider=provider,
            path=extracted_path,
            source_url=source_url,
            archive_path=archive_path,
            extracted_from_archive=True,
            size_bytes=extracted_path.stat().st_size,
        )
    if Path(file_name).suffix.lower() not in SUPPORTED_DOWNLOADED_SUBTITLE_SUFFIXES:
        file_name = file_name + ".srt"
    output_path = output_dir / file_name
    ensure_can_write_download(output_path, force)
    output_path.write_bytes(payload)
    return DownloadedSubtitle(
        provider=provider,
        path=output_path,
        source_url=source_url,
        extracted_from_archive=False,
        size_bytes=len(payload),
    )


def extract_first_subtitle_from_zip(
    archive_path: Path,
    output_dir: Path,
    force: bool = False,
    target_season: Optional[int] = None,
    target_episode: Optional[int] = None,
) -> Path:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _supported_zip_subtitle_members(archive)
            selected = _select_zip_subtitle_member(members, target_season, target_episode)
            if selected is not None:
                _validate_zip_subtitle_member(selected)
                member_name = PurePosixPath(selected.filename)
                output_path = output_dir / sanitized_filename(member_name.name)
                ensure_can_write_download(output_path, force)
                with archive.open(selected) as source:
                    payload = _read_limited(source, MAX_EXTRACTED_SUBTITLE_BYTES, "extracted subtitle")
                output_path.write_bytes(payload)
                return output_path
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as error:
        if isinstance(error, SubtitleProviderApiError):
            raise
        raise SubtitleProviderApiError("downloaded zip could not be safely extracted") from error
    raise SubtitleProviderApiError("downloaded zip did not contain a supported subtitle file")


def _supported_zip_subtitle_members(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    if len(archive.infolist()) > MAX_ZIP_MEMBERS:
        raise SubtitleProviderApiError(f"downloaded zip exceeded the {MAX_ZIP_MEMBERS}-member limit")
    members = []
    for member in archive.infolist():
        if member.is_dir():
            continue
        member_name = PurePosixPath(member.filename)
        if any(part in {"", ".", ".."} for part in member_name.parts):
            continue
        if member_name.suffix.lower() not in SUPPORTED_DOWNLOADED_SUBTITLE_SUFFIXES:
            continue
        members.append(member)
    return members


def _validate_zip_subtitle_member(member: zipfile.ZipInfo) -> None:
    if member.file_size > MAX_EXTRACTED_SUBTITLE_BYTES:
        raise SubtitleProviderApiError(
            f"extracted subtitle exceeded the {MAX_EXTRACTED_SUBTITLE_BYTES}-byte limit"
        )
    if member.file_size and (
        member.compress_size <= 0 or member.file_size / member.compress_size > MAX_ZIP_COMPRESSION_RATIO
    ):
        raise SubtitleProviderApiError("downloaded zip subtitle had an unsafe compression ratio")


def _select_zip_subtitle_member(
    members: List[zipfile.ZipInfo],
    target_season: Optional[int],
    target_episode: Optional[int],
) -> Optional[zipfile.ZipInfo]:
    if not members:
        return None
    if target_season is None or target_episode is None:
        return _preferred_zip_subtitle_member(members)
    matching = [
        member
        for member in members
        if _filename_matches_episode(PurePosixPath(member.filename).name, target_season, target_episode)
    ]
    if matching:
        return _preferred_zip_subtitle_member(matching)
    if len(members) == 1:
        return members[0]
    raise SubtitleProviderApiError(
        "downloaded zip did not contain a subtitle file for S%02dE%02d" % (target_season, target_episode)
    )


def _preferred_zip_subtitle_member(members: List[zipfile.ZipInfo]) -> zipfile.ZipInfo:
    indexed = list(enumerate(members))
    return min(
        indexed,
        key=lambda item: (
            DOWNLOADED_SUBTITLE_SUFFIX_PREFERENCE.get(PurePosixPath(item[1].filename).suffix.lower(), 99),
            item[0],
        ),
    )[1]


def _filename_matches_episode(file_name: str, season: int, episode: int) -> bool:
    value = file_name.lower()
    sxe = re.search(r"(?:^|[^a-z0-9])s(\d{1,2})[^a-z0-9]*e(\d{1,3})(?:[^a-z0-9]|$)", value)
    if sxe and int(sxe.group(1)) == season and int(sxe.group(2)) == episode:
        return True
    numeric = re.search(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})(?:[^a-z0-9]|$)", value)
    return bool(numeric and int(numeric.group(1)) == season and int(numeric.group(2)) == episode)


def ensure_can_write_download(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError("download output already exists: %s" % path)


def is_zip_download(file_name: str, payload: bytes) -> bool:
    return Path(file_name).suffix.lower() == ".zip" or payload.startswith(b"PK\x03\x04")


def archive_filename(file_name: str) -> str:
    path = Path(file_name)
    if path.suffix.lower() == ".zip":
        return file_name
    if path.suffix:
        return path.with_suffix(".zip").name
    return file_name + ".zip"


def url_filename(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    name = posixpath.basename(urllib.parse.urlparse(url).path)
    return name or None


def sanitized_filename(file_name: str) -> str:
    name = file_name.replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(
        character if character.isalnum() or character in {" ", ".", "-", "_", "[", "]", "(", ")"} else "_"
        for character in name
    )
    cleaned = cleaned.strip(" .")
    return cleaned or "subtitle.srt"
