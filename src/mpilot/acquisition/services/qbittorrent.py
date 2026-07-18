from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import posixpath
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
import qbittorrentapi

from mpilot.acquisition.config import Settings
from mpilot.acquisition.domain.torrent_metadata import parse_torrent_info_hash
from mpilot.acquisition.exceptions import SharedDownloadControlError, UpstreamServiceError
from mpilot.acquisition.models import TorrentStatus


logger = logging.getLogger("qbitlarr-api.qbittorrent")
_TORRENT_FILE_CACHE_MAX_ENTRIES = 200
_TORRENT_FILE_MAX_BYTES = 10 * 1024 * 1024
_TORRENT_FILE_MAX_REDIRECTS = 5
_TORRENT_FILE_CACHE: OrderedDict[str, bytes] = OrderedDict()
_MANAGED_TAG = "qbitlarr.managed"
_REQUESTER_TAG_PREFIX = "requester."
_REQUESTER_TAG_MAX_LENGTH = 64
_TORRENT_COMPLETE_STATES = {"uploading", "stalledUP", "pausedUP", "forcedUP", "queuedUP"}


@dataclass(frozen=True)
class TorrentAddPayload:
    kwargs: dict
    info_hash: str | None = None


class _TorrentPeerLinkRedirect(UpstreamServiceError):
    def __init__(self, download_link: str):
        super().__init__("Torrent URL redirected to a peer link")
        self.download_link = download_link


async def add_download_to_qbittorrent(
    download_link: str,
    settings: Settings,
    *,
    save_path: str | None = None,
    requester_id: str | None = None,
) -> TorrentStatus | None:
    payload = await _build_torrent_add_payload(download_link, settings)
    requester_tag = _requester_tag_for_user(requester_id)
    torrent_tags = _torrent_tags_for_add(requester_tag)

    def add_download_sync() -> tuple[str, TorrentStatus | None]:
        with qbittorrentapi.Client(
            host=settings.qbit_url,
            username=settings.qbit_username,
            password=settings.qbit_password,
        ) as qbit_client:
            qbit_client.auth_log_in()
            if payload.info_hash and _torrent_exists(qbit_client, payload.info_hash):
                _apply_qbitlarr_tags(qbit_client, payload.info_hash, requester_tag)
                _apply_retention_policy(qbit_client, payload.info_hash, settings)
                logger.info("qBittorrent already has torrent hash=%s", payload.info_hash)
                return "Ok.", _get_torrent_status(qbit_client, payload.info_hash)

            add_kwargs = dict(payload.kwargs)
            add_kwargs["tags"] = torrent_tags

            result = qbit_client.torrents_add(**add_kwargs, save_path=save_path)
            if str(result).strip().lower() != "ok." and payload.info_hash and _torrent_exists(
                qbit_client,
                payload.info_hash,
            ):
                _apply_qbitlarr_tags(qbit_client, payload.info_hash, requester_tag)
                _apply_retention_policy(qbit_client, payload.info_hash, settings)
                logger.info("qBittorrent add result was non-OK but torrent hash=%s exists", payload.info_hash)
                return "Ok.", _get_torrent_status(qbit_client, payload.info_hash)
            if payload.info_hash:
                _apply_qbitlarr_tags(qbit_client, payload.info_hash, requester_tag)
                _apply_retention_policy(qbit_client, payload.info_hash, settings)
            return str(result), _get_torrent_status(qbit_client, payload.info_hash)

    try:
        result, status = await asyncio.to_thread(add_download_sync)
    except qbittorrentapi.LoginFailed as exc:
        logger.warning("qBittorrent login failed")
        raise UpstreamServiceError("qBittorrent login failed") from exc
    except qbittorrentapi.APIConnectionError as exc:
        logger.warning("qBittorrent request failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent is unreachable") from exc
    except qbittorrentapi.APIError as exc:
        logger.warning("qBittorrent API error: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent rejected the download") from exc

    if result.strip().lower() != "ok.":
        logger.warning("qBittorrent returned an unexpected add result")
        raise UpstreamServiceError("qBittorrent returned an unexpected response")

    logger.info("qBittorrent accepted download mode=%s save_path=%s", _add_mode(payload.kwargs), save_path or "default")
    return status


async def _build_torrent_add_payload(download_link: str, settings: Settings) -> TorrentAddPayload:
    if _should_upload_torrent_file(download_link):
        try:
            content = await _download_torrent_file(download_link, settings)
        except _TorrentPeerLinkRedirect as redirect:
            return TorrentAddPayload(
                kwargs={"urls": redirect.download_link},
                info_hash=_info_hash_from_magnet(redirect.download_link),
            )
        info_hash = parse_torrent_info_hash(content)
        if not info_hash:
            raise UpstreamServiceError("Torrent URL returned invalid torrent data")
        return TorrentAddPayload(
            kwargs={"torrent_files": content},
            info_hash=info_hash,
        )

    return TorrentAddPayload(
        kwargs={"urls": download_link},
        info_hash=_info_hash_from_magnet(download_link),
    )


def _should_upload_torrent_file(download_link: str) -> bool:
    return urlparse(download_link).scheme.lower() in {"http", "https"}


async def _download_torrent_file(download_link: str, settings: Settings) -> bytes:
    fetch_url = _download_url_with_prowlarr_api_key(download_link, settings)
    _validate_torrent_fetch_url(fetch_url, settings)
    if fetch_url in _TORRENT_FILE_CACHE:
        _TORRENT_FILE_CACHE.move_to_end(fetch_url)
        return _TORRENT_FILE_CACHE[fetch_url]

    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=False) as client:
            content = await _fetch_torrent_file(client, fetch_url, settings)
    except httpx.HTTPError as exc:
        logger.warning("Torrent file fetch failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("Torrent file is unreachable") from exc

    _cache_torrent_file(fetch_url, content)
    return content


async def _fetch_torrent_file(client, fetch_url: str, settings: Settings) -> bytes:
    current_url = fetch_url
    for redirect_count in range(_TORRENT_FILE_MAX_REDIRECTS + 1):
        _validate_torrent_fetch_url(current_url, settings)
        async with client.stream("GET", current_url) as response:
            response_url = getattr(response, "url", None)
            if response_url is not None:
                _validate_torrent_fetch_url(str(response_url), settings)

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location or redirect_count >= _TORRENT_FILE_MAX_REDIRECTS:
                    raise UpstreamServiceError("Torrent URL returned an invalid redirect")
                next_url = urljoin(current_url, location)
                if urlparse(next_url).scheme.lower() in {"magnet", "bc"}:
                    raise _TorrentPeerLinkRedirect(next_url)
                _validate_torrent_fetch_url(next_url, settings)
                current_url = next_url
                continue

            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "html" in content_type:
                logger.warning("Torrent file URL returned HTML content")
                raise UpstreamServiceError("Torrent URL returned HTML instead of torrent data")

            chunks = []
            total_bytes = 0
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > _TORRENT_FILE_MAX_BYTES:
                    logger.warning("Torrent file response exceeded size limit")
                    raise UpstreamServiceError("Torrent file response is too large")
                chunks.append(chunk)

            content = b"".join(chunks)
            if not content:
                logger.warning("Torrent file URL returned an empty response")
                raise UpstreamServiceError("Torrent file response was empty")
            return content

    raise UpstreamServiceError("Torrent URL returned too many redirects")


def _validate_torrent_fetch_url(fetch_url: str, settings: Settings) -> None:
    parsed = urlparse(fetch_url)
    origin = _torrent_url_origin(parsed)
    if origin is None or origin not in _allowed_torrent_download_origins(settings):
        raise UpstreamServiceError("Torrent URL is not allowed")


def _allowed_torrent_download_origins(settings: Settings) -> set[tuple[str, str, int]]:
    origins = set()
    for configured_url in (
        getattr(settings, "prowlarr_url", None),
        getattr(settings, "prowlarr_download_url", None),
    ):
        if not isinstance(configured_url, str) or not configured_url.strip():
            continue
        origin = _torrent_url_origin(urlparse(configured_url))
        if origin is not None:
            origins.add(origin)
    return origins


def _torrent_url_origin(parsed) -> tuple[str, str, int] | None:
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return scheme, parsed.hostname.casefold(), port or (443 if scheme == "https" else 80)


def _cache_torrent_file(fetch_url: str, content: bytes) -> None:
    _TORRENT_FILE_CACHE[fetch_url] = content
    _TORRENT_FILE_CACHE.move_to_end(fetch_url)
    while len(_TORRENT_FILE_CACHE) > _TORRENT_FILE_CACHE_MAX_ENTRIES:
        _TORRENT_FILE_CACHE.popitem(last=False)


def _download_url_with_prowlarr_api_key(download_link: str, settings: Settings) -> str:
    parsed = urlparse(download_link)
    if parsed.scheme.lower() not in {"http", "https"}:
        return download_link
    if not _is_prowlarr_download_link(parsed, settings):
        return download_link

    api_key = getattr(settings, "prowlarr_api_key", None)
    if not api_key:
        return download_link

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() == "apikey" for key, _ in query_pairs):
        return download_link

    query_pairs.append(("apikey", api_key))
    return urlunparse(parsed._replace(query=urlencode(query_pairs)))


def _is_prowlarr_download_link(parsed, settings: Settings) -> bool:
    prowlarr_urls = [
        getattr(settings, "prowlarr_url", None),
        getattr(settings, "prowlarr_download_url", None),
    ]
    prowlarr_netlocs = {urlparse(url).netloc for url in prowlarr_urls if isinstance(url, str) and url.strip()}
    return parsed.netloc in prowlarr_netlocs and (
        parsed.path.startswith("/api/")
        or parsed.path.endswith("/download")
        or "/download/" in parsed.path
    )


def _info_hash_from_magnet(download_link: str) -> str | None:
    parsed = urlparse(download_link)
    if parsed.scheme.lower() != "magnet":
        return None

    for value in parse_qs(parsed.query).get("xt", []):
        if value.casefold().startswith("urn:btih:"):
            return _normalize_btih_hash(value.rsplit(":", 1)[-1])

    return None


def _torrent_exists(qbit_client, info_hash: str) -> bool:
    return _get_torrent_status(qbit_client, info_hash) is not None


def _get_torrent_status(qbit_client, info_hash: str | None, *, tag: str | None = None) -> TorrentStatus | None:
    if not info_hash:
        return None

    target = _normalize_btih_hash(info_hash)
    for torrent in _list_torrents(qbit_client, torrent_hashes=target, tag=tag):
        if str(torrent.hash).casefold() == target:
            return _torrent_status_from_client_torrent(torrent)
    return None


def _get_torrent(qbit_client, info_hash: str, *, tag: str | None = None):
    target = _normalize_btih_hash(info_hash)
    for torrent in _list_torrents(qbit_client, torrent_hashes=target, tag=tag):
        if str(torrent.hash).casefold() == target:
            return torrent
    return None


def _normalize_btih_hash(value: str) -> str:
    stripped = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", stripped):
        return stripped.casefold()
    if re.fullmatch(r"[A-Z2-7a-z]{32}", stripped):
        try:
            return base64.b32decode(stripped.upper()).hex()
        except (binascii.Error, ValueError):
            return stripped.casefold()
    return stripped.casefold()


def _torrent_status_from_client_torrent(torrent) -> TorrentStatus:
    return TorrentStatus(
        name=torrent.name,
        state=torrent.state,
        progress=round(torrent.progress, 4),
        size=torrent.size,
        seeds=torrent.num_seeds,
        hash=torrent.hash,
        download_speed=_optional_int(getattr(torrent, "dlspeed", None)),
        eta=_optional_int(getattr(torrent, "eta", None)),
        content_path=_torrent_content_path(torrent) if _torrent_is_complete(torrent) else None,
    )


def _torrent_is_complete(torrent) -> bool:
    try:
        progress = float(getattr(torrent, "progress", 0.0) or 0.0)
    except (TypeError, ValueError):
        progress = 0.0
    return progress >= 1.0 or str(getattr(torrent, "state", "")) in _TORRENT_COMPLETE_STATES


def _torrent_content_path(torrent) -> str | None:
    content_path = _optional_str(getattr(torrent, "content_path", None))
    if content_path:
        return content_path

    save_path = _optional_str(getattr(torrent, "save_path", None))
    name = _optional_str(getattr(torrent, "name", None))
    if not save_path or not name:
        return None
    if name.startswith("/"):
        return name
    return posixpath.join(save_path.rstrip("/"), name)


def _optional_str(value) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _optional_int(value) -> int | None:
    if value is None:
        return None
    return int(value)


def _add_mode(add_kwargs: dict) -> str:
    if "torrent_files" in add_kwargs:
        return "file"
    if str(add_kwargs.get("urls", "")).startswith("magnet:"):
        return "magnet"
    return "url"


def _list_torrents(qbit_client, *, torrent_hashes: str | None = None, tag: str | None = None):
    kwargs = {}
    if torrent_hashes:
        kwargs["torrent_hashes"] = torrent_hashes
    if tag:
        kwargs["tag"] = tag
    return qbit_client.torrents_info(**kwargs)


def _apply_qbitlarr_tags(qbit_client, info_hash: str, requester_tag: str | None) -> None:
    qbit_client.torrents_add_tags(tags=_torrent_tags_for_add(requester_tag), torrent_hashes=info_hash)


def _torrent_tags_for_add(requester_tag: str | None) -> str:
    tags = [_MANAGED_TAG]
    if requester_tag:
        tags.append(requester_tag)
    return ",".join(tags)


def _apply_retention_policy(qbit_client, info_hash: str, settings: Settings) -> None:
    if not getattr(settings, "retention_enabled", False):
        return

    ratio_limit = getattr(settings, "retention_ratio_limit", None)
    seeding_time_limit = getattr(settings, "retention_seeding_time_limit_minutes", None)
    if ratio_limit is None and seeding_time_limit is None:
        return

    try:
        qbit_client.torrents_set_share_limits(
            ratio_limit=ratio_limit,
            seeding_time_limit=seeding_time_limit,
            share_limit_action=getattr(settings, "retention_action", "Remove"),
            torrent_hashes=info_hash,
        )
    except qbittorrentapi.APIError as exc:
        logger.warning(
            "Could not apply retention policy for torrent hash=%s: %s",
            info_hash,
            exc.__class__.__name__,
        )


def _requester_tag_for_user(requester_id: str | None) -> str | None:
    if requester_id is None:
        return None

    normalized = requester_id.strip().casefold()
    if not normalized:
        return None

    sanitized = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("-._")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    suffix_budget = _REQUESTER_TAG_MAX_LENGTH - len(_REQUESTER_TAG_PREFIX) - len(digest) - 1
    trimmed = sanitized[: max(suffix_budget, 1)].rstrip("-._")
    if not trimmed:
        trimmed = "id"
    return f"{_REQUESTER_TAG_PREFIX}{trimmed}-{digest}"


async def list_downloads_from_qbittorrent(
    settings: Settings,
    requester_id: str | None = None,
) -> list[TorrentStatus]:
    requester_tag = _requester_tag_for_user(requester_id)

    def list_sync() -> list[TorrentStatus]:
        with qbittorrentapi.Client(
            host=settings.qbit_url,
            username=settings.qbit_username,
            password=settings.qbit_password,
        ) as qbit_client:
            qbit_client.auth_log_in()
            return [_torrent_status_from_client_torrent(t) for t in _list_torrents(qbit_client, tag=requester_tag)]

    try:
        return await asyncio.to_thread(list_sync)
    except qbittorrentapi.LoginFailed as exc:
        logger.warning("qBittorrent login failed")
        raise UpstreamServiceError("qBittorrent login failed") from exc
    except qbittorrentapi.APIConnectionError as exc:
        logger.warning("qBittorrent request failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent is unreachable") from exc
    except qbittorrentapi.APIError as exc:
        logger.warning("qBittorrent API error: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent API error") from exc


async def get_download_status_from_qbittorrent(
    settings: Settings,
    info_hash: str,
    requester_id: str | None = None,
) -> TorrentStatus | None:
    requester_tag = _requester_tag_for_user(requester_id)

    def get_status_sync() -> TorrentStatus | None:
        with qbittorrentapi.Client(
            host=settings.qbit_url,
            username=settings.qbit_username,
            password=settings.qbit_password,
        ) as qbit_client:
            qbit_client.auth_log_in()
            return _get_torrent_status(qbit_client, info_hash, tag=requester_tag)

    try:
        return await asyncio.to_thread(get_status_sync)
    except qbittorrentapi.LoginFailed as exc:
        logger.warning("qBittorrent login failed")
        raise UpstreamServiceError("qBittorrent login failed") from exc
    except qbittorrentapi.APIConnectionError as exc:
        logger.warning("qBittorrent request failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent is unreachable") from exc
    except qbittorrentapi.APIError as exc:
        logger.warning("qBittorrent API error: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent API error") from exc


async def pause_download_in_qbittorrent(
    settings: Settings,
    info_hash: str,
    requester_id: str,
) -> TorrentStatus | None:
    return await _control_download_in_qbittorrent(
        settings,
        info_hash,
        requester_id=requester_id,
        action="pause",
    )


async def resume_download_in_qbittorrent(
    settings: Settings,
    info_hash: str,
    requester_id: str,
) -> TorrentStatus | None:
    return await _control_download_in_qbittorrent(
        settings,
        info_hash,
        requester_id=requester_id,
        action="resume",
    )


async def delete_download_from_qbittorrent(
    settings: Settings,
    info_hash: str,
    requester_id: str,
) -> TorrentStatus | None:
    return await _control_download_in_qbittorrent(
        settings,
        info_hash,
        requester_id=requester_id,
        action="delete",
    )


async def _control_download_in_qbittorrent(
    settings: Settings,
    info_hash: str,
    *,
    requester_id: str,
    action: str,
) -> TorrentStatus | None:
    requester_tag = _requester_tag_for_user(requester_id)
    if not requester_tag:
        return None

    def control_sync() -> TorrentStatus | None:
        with qbittorrentapi.Client(
            host=settings.qbit_url,
            username=settings.qbit_username,
            password=settings.qbit_password,
        ) as qbit_client:
            qbit_client.auth_log_in()
            torrent = _get_torrent(qbit_client, info_hash, tag=requester_tag)
            if torrent is None:
                return None
            status = _torrent_status_from_client_torrent(torrent)
            requester_tags = {
                tag for tag in _torrent_tags(getattr(torrent, "tags", "")) if tag.startswith(_REQUESTER_TAG_PREFIX)
            }
            if len(requester_tags) > 1:
                raise SharedDownloadControlError(
                    "Download is shared by multiple requesters and cannot be controlled by one requester"
                )
            if action == "pause":
                qbit_client.torrents_pause(torrent_hashes=info_hash)
            elif action == "resume":
                qbit_client.torrents_resume(torrent_hashes=info_hash)
            elif action == "delete":
                qbit_client.torrents_delete(delete_files=False, torrent_hashes=info_hash)
                return status
            else:
                raise ValueError(f"Unsupported download control action: {action}")
            return _get_torrent_status(qbit_client, info_hash, tag=requester_tag) or status

    try:
        return await asyncio.to_thread(control_sync)
    except qbittorrentapi.LoginFailed as exc:
        logger.warning("qBittorrent login failed")
        raise UpstreamServiceError("qBittorrent login failed") from exc
    except qbittorrentapi.APIConnectionError as exc:
        logger.warning("qBittorrent request failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent is unreachable") from exc
    except qbittorrentapi.APIError as exc:
        logger.warning("qBittorrent API error: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent API error") from exc


async def tag_download_for_requester(
    settings: Settings,
    info_hash: str,
    requester_id: str | None,
) -> str | None:
    requester_tag = _requester_tag_for_user(requester_id)
    if not requester_tag:
        return None

    def tag_sync() -> None:
        with qbittorrentapi.Client(
            host=settings.qbit_url,
            username=settings.qbit_username,
            password=settings.qbit_password,
        ) as qbit_client:
            qbit_client.auth_log_in()
            _apply_qbitlarr_tags(qbit_client, info_hash, requester_tag)

    try:
        await asyncio.to_thread(tag_sync)
    except qbittorrentapi.LoginFailed as exc:
        logger.warning("qBittorrent login failed")
        raise UpstreamServiceError("qBittorrent login failed") from exc
    except qbittorrentapi.APIConnectionError as exc:
        logger.warning("qBittorrent request failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent is unreachable") from exc
    except qbittorrentapi.APIError as exc:
        logger.warning("qBittorrent API error: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent API error") from exc
    return requester_tag


async def cleanup_completed_downloads_from_qbittorrent(
    settings: Settings,
    *,
    now: float | None = None,
) -> dict:
    if not getattr(settings, "cleanup_enabled", False):
        return {"status": "disabled", "deleted_count": 0, "deleted_hashes": []}

    current_time = int(now if now is not None else time.time())
    completed_after_seconds = int(getattr(settings, "cleanup_completed_after_seconds", 259_200))
    include_legacy_requester_tags = bool(getattr(settings, "cleanup_include_legacy_requester_tags", True))

    def cleanup_sync() -> dict:
        with qbittorrentapi.Client(
            host=settings.qbit_url,
            username=settings.qbit_username,
            password=settings.qbit_password,
        ) as qbit_client:
            qbit_client.auth_log_in()
            hashes_to_delete = [
                str(torrent.hash)
                for torrent in _list_torrents(qbit_client)
                if _should_cleanup_torrent(
                    torrent,
                    now=current_time,
                    completed_after_seconds=completed_after_seconds,
                    include_legacy_requester_tags=include_legacy_requester_tags,
                )
            ]
            if hashes_to_delete:
                qbit_client.torrents_delete(delete_files=False, torrent_hashes="|".join(hashes_to_delete))
            return {
                "status": "success",
                "deleted_count": len(hashes_to_delete),
                "deleted_hashes": hashes_to_delete,
            }

    try:
        return await asyncio.to_thread(cleanup_sync)
    except qbittorrentapi.LoginFailed as exc:
        logger.warning("qBittorrent login failed")
        raise UpstreamServiceError("qBittorrent login failed") from exc
    except qbittorrentapi.APIConnectionError as exc:
        logger.warning("qBittorrent request failed: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent is unreachable") from exc
    except qbittorrentapi.APIError as exc:
        logger.warning("qBittorrent API error: %s", exc.__class__.__name__)
        raise UpstreamServiceError("qBittorrent API error") from exc


def _should_cleanup_torrent(
    torrent,
    *,
    now: int,
    completed_after_seconds: int,
    include_legacy_requester_tags: bool,
) -> bool:
    if not _is_qbitlarr_managed_torrent(torrent, include_legacy_requester_tags=include_legacy_requester_tags):
        return False
    completion_on = _optional_int(getattr(torrent, "completion_on", None))
    if completion_on is None or completion_on <= 0:
        return False
    if _progress_value(getattr(torrent, "progress", None)) < 1.0:
        return False
    return completion_on <= now - completed_after_seconds


def _is_qbitlarr_managed_torrent(torrent, *, include_legacy_requester_tags: bool) -> bool:
    tags = _torrent_tags(getattr(torrent, "tags", ""))
    if _MANAGED_TAG in tags:
        return True
    return include_legacy_requester_tags and any(tag.startswith(_REQUESTER_TAG_PREFIX) for tag in tags)


def _torrent_tags(value) -> set[str]:
    if isinstance(value, str):
        return {tag.strip() for tag in value.split(",") if tag.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(tag).strip() for tag in value if str(tag).strip()}
    return set()


def _progress_value(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def check_qbittorrent_health(settings: Settings) -> dict[str, str]:
    def check_sync() -> None:
        with qbittorrentapi.Client(
            host=settings.qbit_url,
            username=settings.qbit_username,
            password=settings.qbit_password,
        ) as qbit_client:
            qbit_client.auth_log_in()

    try:
        await asyncio.to_thread(check_sync)
    except qbittorrentapi.LoginFailed:
        return {"status": "error", "detail": "qBittorrent login failed"}
    except qbittorrentapi.APIConnectionError:
        return {"status": "error", "detail": "qBittorrent is unreachable"}
    except qbittorrentapi.APIError:
        return {"status": "error", "detail": "qBittorrent API error"}
    return {"status": "ok"}
