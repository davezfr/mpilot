from __future__ import annotations

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest

from mpilot.acquisition.exceptions import SharedDownloadControlError, UpstreamServiceError
from mpilot.acquisition.services.qbittorrent import (
    _TORRENT_FILE_CACHE,
    _TORRENT_FILE_CACHE_MAX_ENTRIES,
    _requester_tag_for_user,
    add_download_to_qbittorrent,
    cleanup_completed_downloads_from_qbittorrent,
    delete_download_from_qbittorrent,
    get_download_status_from_qbittorrent,
    list_downloads_from_qbittorrent,
    pause_download_in_qbittorrent,
    resume_download_in_qbittorrent,
)


class FakeQbittorrentClient:
    calls: list[dict] = []
    tag_calls: list[dict] = []
    pause_calls: list[dict] = []
    resume_calls: list[dict] = []
    delete_calls: list[dict] = []
    share_limit_calls: list[dict] = []
    existing_hashes: list[str] = []
    hashes_after_add: list[str] = []
    add_result = "Ok."
    torrent_tags_by_hash: dict[str, set[str]] = {}
    torrent_overrides_by_hash: dict[str, dict] = {}

    def __init__(self, *, host, username, password):
        self.host = host
        self.username = username
        self.password = password

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def auth_log_in(self):
        return None

    def torrents_add(self, **kwargs):
        self.calls.append(kwargs)
        self.existing_hashes = list(self.hashes_after_add)
        return self.add_result

    def torrents_add_tags(self, *, tags=None, torrent_hashes=None, **kwargs):
        self.tag_calls.append({"tags": tags, "torrent_hashes": torrent_hashes})
        if tags and torrent_hashes:
            normalized_hash = str(torrent_hashes).casefold()
            self.torrent_tags_by_hash.setdefault(normalized_hash, set()).update(
                tag.strip() for tag in str(tags).split(",") if tag.strip()
            )

    def torrents_set_share_limits(
        self,
        *,
        ratio_limit=None,
        seeding_time_limit=None,
        share_limit_action=None,
        torrent_hashes=None,
        **kwargs,
    ):
        self.share_limit_calls.append(
            {
                "ratio_limit": ratio_limit,
                "seeding_time_limit": seeding_time_limit,
                "share_limit_action": share_limit_action,
                "torrent_hashes": torrent_hashes,
            }
        )

    def torrents_pause(self, *, torrent_hashes=None, **kwargs):
        self.pause_calls.append({"torrent_hashes": torrent_hashes})
        if torrent_hashes:
            self.torrent_overrides_by_hash.setdefault(str(torrent_hashes).casefold(), {})["state"] = "pausedDL"

    def torrents_resume(self, *, torrent_hashes=None, **kwargs):
        self.resume_calls.append({"torrent_hashes": torrent_hashes})
        if torrent_hashes:
            self.torrent_overrides_by_hash.setdefault(str(torrent_hashes).casefold(), {})["state"] = "downloading"

    def torrents_delete(self, *, delete_files=None, torrent_hashes=None, **kwargs):
        self.delete_calls.append({"delete_files": delete_files, "torrent_hashes": torrent_hashes})

    def torrents_info(self, torrent_hashes=None, tag=None, **kwargs):
        hashes = list(self.existing_hashes)
        if torrent_hashes:
            target = str(torrent_hashes).casefold()
            hashes = [value for value in hashes if str(value).casefold() == target]
        if tag:
            hashes = [
                value
                for value in hashes
                if str(tag) in self.torrent_tags_by_hash.get(str(value).casefold(), set())
            ]
        return [_fake_torrent(value, **self.torrent_overrides_by_hash.get(str(value).casefold(), {})) for value in hashes]


INFO_DICT = b"d4:name4:Teste"
INFO_HASH = hashlib.sha1(INFO_DICT).hexdigest()
TORRENT_CONTENT = b"d8:announce15:https://tracker4:info" + INFO_DICT + b"e"


def _fake_torrent(hash_value=INFO_HASH, **overrides):
    tags = overrides.pop("tags", None)
    if tags is None:
        tags = ",".join(sorted(FakeQbittorrentClient.torrent_tags_by_hash.get(str(hash_value).casefold(), set())))
    return SimpleNamespace(
        hash=hash_value,
        name=overrides.pop("name", "Test"),
        state=overrides.pop("state", "downloading"),
        progress=overrides.pop("progress", 0.25),
        size=overrides.pop("size", 1_000_000_000),
        num_seeds=overrides.pop("num_seeds", 7),
        dlspeed=overrides.pop("dlspeed", 2_000_000),
        eta=overrides.pop("eta", 600),
        content_path=overrides.pop("content_path", None),
        save_path=overrides.pop("save_path", None),
        tags=tags,
        ratio=overrides.pop("ratio", 0.0),
        ratio_limit=overrides.pop("ratio_limit", -2),
        seeding_time=overrides.pop("seeding_time", 0),
        seeding_time_limit=overrides.pop("seeding_time_limit", -2),
        share_limit_action=overrides.pop("share_limit_action", None),
        added_on=overrides.pop("added_on", 0),
        completion_on=overrides.pop("completion_on", -1),
    )


class FakeTorrentResponse:
    def __init__(
        self,
        *,
        content=TORRENT_CONTENT,
        headers=None,
        status_code=200,
        url=None,
    ):
        self.content = content
        self.headers = headers or {"content-type": "application/x-bittorrent"}
        self.status_code = status_code
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self.content


class FakeAsyncClient:
    fetched_urls: list[str] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def stream(self, method, url):
        assert method == "GET"
        self.fetched_urls.append(url)
        return FakeTorrentResponse(url=url)


class FakeLargeTorrentResponse:
    headers = {"content-type": "application/x-bittorrent"}
    status_code = 200
    chunks_yielded = 0

    def __init__(self, *, url=None):
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in (b"x" * (6 * 1024 * 1024), b"y" * (6 * 1024 * 1024), b"not-read"):
            type(self).chunks_yielded += 1
            yield chunk


class FakeLargeAsyncClient(FakeAsyncClient):
    def stream(self, method, url):
        assert method == "GET"
        self.fetched_urls.append(url)
        return FakeLargeTorrentResponse(url=url)


class FakeInvalidTorrentAsyncClient(FakeAsyncClient):
    def stream(self, method, url):
        assert method == "GET"
        self.fetched_urls.append(url)
        return FakeTorrentResponse(content=b"not-bencoded-torrent-data", url=url)


class FakeRedirectAsyncClient(FakeAsyncClient):
    redirect_location = "http://127.0.0.1:8080/internal"

    def stream(self, method, url):
        assert method == "GET"
        self.fetched_urls.append(url)
        return FakeTorrentResponse(
            headers={"location": self.redirect_location},
            status_code=302,
            url=url,
        )


def _settings():
    return SimpleNamespace(
        prowlarr_url="http://prowlarr.test",
        prowlarr_download_url=None,
        prowlarr_api_key="secret",
        qbit_url="http://qbit.test",
        qbit_username="user",
        qbit_password="pass",
        request_timeout_seconds=30,
        retention_enabled=False,
        retention_ratio_limit=2.0,
        retention_seeding_time_limit_minutes=10080,
        retention_action="Remove",
        cleanup_enabled=False,
        cleanup_completed_after_seconds=259_200,
        cleanup_interval_seconds=21_600,
        cleanup_include_legacy_requester_tags=True,
    )


def _reset_fakes():
    FakeQbittorrentClient.calls = []
    FakeQbittorrentClient.tag_calls = []
    FakeQbittorrentClient.pause_calls = []
    FakeQbittorrentClient.resume_calls = []
    FakeQbittorrentClient.delete_calls = []
    FakeQbittorrentClient.share_limit_calls = []
    FakeQbittorrentClient.existing_hashes = []
    FakeQbittorrentClient.hashes_after_add = []
    FakeQbittorrentClient.add_result = "Ok."
    FakeQbittorrentClient.torrent_tags_by_hash = {}
    FakeQbittorrentClient.torrent_overrides_by_hash = {}
    FakeAsyncClient.fetched_urls = []
    FakeLargeTorrentResponse.chunks_yielded = 0
    _TORRENT_FILE_CACHE.clear()


def test_add_download_uploads_http_torrent_content(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        add_download_to_qbittorrent(
            "http://prowlarr.test/1/download?link=abc",
            _settings(),
            save_path="/downloads/movies/",
        )
    )

    assert FakeAsyncClient.fetched_urls == ["http://prowlarr.test/1/download?link=abc&apikey=secret"]
    assert FakeQbittorrentClient.calls == [
        {
            "torrent_files": TORRENT_CONTENT,
            "tags": "qbitlarr.managed",
            "save_path": "/downloads/movies/",
        }
    ]


def test_add_download_passes_magnets_as_urls(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(add_download_to_qbittorrent("magnet:?xt=urn:btih:abcdef", _settings()))

    assert FakeAsyncClient.fetched_urls == []
    assert FakeQbittorrentClient.calls == [
        {
            "urls": "magnet:?xt=urn:btih:abcdef",
            "tags": "qbitlarr.managed",
            "save_path": None,
        }
    ]


def test_add_download_detects_existing_base32_magnet_hash(monkeypatch):
    _reset_fakes()
    base32_hash = base64.b32encode(bytes.fromhex(INFO_HASH)).decode("ascii").rstrip("=")
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    status = asyncio.run(add_download_to_qbittorrent(f"magnet:?xt=urn:btih:{base32_hash}", _settings()))

    assert status is not None
    assert status.hash == INFO_HASH
    assert FakeQbittorrentClient.calls == []


def test_add_download_skips_qbittorrent_add_when_torrent_already_exists(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH.upper()]
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(add_download_to_qbittorrent("http://prowlarr.test/1/download?link=abc", _settings()))

    assert FakeAsyncClient.fetched_urls == ["http://prowlarr.test/1/download?link=abc&apikey=secret"]
    assert FakeQbittorrentClient.calls == []


def test_add_download_treats_duplicate_result_as_success_when_torrent_exists_after_add(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.hashes_after_add = [INFO_HASH]
    FakeQbittorrentClient.add_result = "Fails."
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        add_download_to_qbittorrent(
            "http://prowlarr.test/1/download?link=abc",
            _settings(),
            save_path="/downloads/movies/",
        )
    )

    assert FakeQbittorrentClient.calls == [
        {
            "torrent_files": TORRENT_CONTENT,
            "tags": "qbitlarr.managed",
            "save_path": "/downloads/movies/",
        }
    ]


def test_add_download_does_not_duplicate_existing_prowlarr_api_key(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(add_download_to_qbittorrent("http://prowlarr.test/1/download?link=abc&apikey=already", _settings()))

    assert FakeAsyncClient.fetched_urls == ["http://prowlarr.test/1/download?link=abc&apikey=already"]


def test_add_download_rejects_unconfigured_http_torrent_origin_before_fetch(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    with pytest.raises(UpstreamServiceError, match="not allowed"):
        asyncio.run(add_download_to_qbittorrent("http://127.0.0.1:9696/download/1.torrent", _settings()))

    assert FakeAsyncClient.fetched_urls == []
    assert FakeQbittorrentClient.calls == []


def test_add_download_allows_explicitly_configured_loopback_prowlarr(monkeypatch):
    _reset_fakes()
    settings = _settings()
    settings.prowlarr_url = "http://127.0.0.1:9696"
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(add_download_to_qbittorrent("http://127.0.0.1:9696/download/1.torrent", settings))

    assert FakeAsyncClient.fetched_urls == ["http://127.0.0.1:9696/download/1.torrent?apikey=secret"]
    assert FakeQbittorrentClient.calls[0]["torrent_files"] == TORRENT_CONTENT


@pytest.mark.parametrize(
    "download_link",
    [
        "http://prowlarr.test:22/internal",
        "https://prowlarr.test/internal",
        "http://user@prowlarr.test/internal",
    ],
)
def test_add_download_rejects_scheme_port_or_credentials_outside_configured_origin(monkeypatch, download_link):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    with pytest.raises(UpstreamServiceError, match="not allowed"):
        asyncio.run(add_download_to_qbittorrent(download_link, _settings()))

    assert FakeAsyncClient.fetched_urls == []
    assert FakeQbittorrentClient.calls == []


def test_add_download_rejects_cross_origin_redirect_before_following_it(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeRedirectAsyncClient)

    with pytest.raises(UpstreamServiceError, match="not allowed"):
        asyncio.run(add_download_to_qbittorrent("http://prowlarr.test/1/download", _settings()))

    assert FakeAsyncClient.fetched_urls == ["http://prowlarr.test/1/download?apikey=secret"]
    assert FakeQbittorrentClient.calls == []


def test_add_download_rejects_oversized_http_torrent_response(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeLargeAsyncClient)

    with pytest.raises(UpstreamServiceError, match="too large"):
        asyncio.run(add_download_to_qbittorrent("http://prowlarr.test/1/download?link=abc", _settings()))

    assert FakeLargeTorrentResponse.chunks_yielded == 2
    assert FakeQbittorrentClient.calls == []


def test_add_download_rejects_invalid_torrent_payload(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeInvalidTorrentAsyncClient)

    with pytest.raises(UpstreamServiceError, match="invalid torrent data"):
        asyncio.run(add_download_to_qbittorrent("http://prowlarr.test/1/download", _settings()))

    assert FakeQbittorrentClient.calls == []


def test_torrent_file_cache_evicts_oldest_entry_when_full(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    for index in range(_TORRENT_FILE_CACHE_MAX_ENTRIES):
        _TORRENT_FILE_CACHE[f"http://prowlarr.test/{index}/download?apikey=secret"] = TORRENT_CONTENT

    asyncio.run(add_download_to_qbittorrent("http://prowlarr.test/new/download", _settings()))

    assert len(_TORRENT_FILE_CACHE) == _TORRENT_FILE_CACHE_MAX_ENTRIES
    assert "http://prowlarr.test/0/download?apikey=secret" not in _TORRENT_FILE_CACHE
    assert "http://prowlarr.test/new/download?apikey=secret" in _TORRENT_FILE_CACHE


def test_add_download_returns_qbittorrent_status_for_added_torrent(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.hashes_after_add = [INFO_HASH]
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    status = asyncio.run(add_download_to_qbittorrent("http://prowlarr.test/1/download?link=abc", _settings()))

    assert status is not None
    assert status.hash == INFO_HASH
    assert status.name == "Test"
    assert status.progress == 0.25
    assert status.download_speed == 2_000_000
    assert status.eta == 600


def test_torrent_status_falls_back_to_save_path_and_name_for_content_path(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_overrides_by_hash = {
        INFO_HASH: {
            "name": "Example.Movie.2026.1080p.WEB-DL-GRP.mkv",
            "state": "uploading",
            "progress": 1.0,
            "save_path": "/media/Movies",
            "content_path": None,
        }
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    status = asyncio.run(get_download_status_from_qbittorrent(_settings(), INFO_HASH))

    assert status is not None
    assert status.content_path == "/media/Movies/Example.Movie.2026.1080p.WEB-DL-GRP.mkv"


def test_incomplete_torrent_status_does_not_expose_future_content_path(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_overrides_by_hash = {
        INFO_HASH: {
            "name": "Example.Movie.2026.1080p.WEB-DL-GRP.mkv",
            "state": "downloading",
            "progress": 0.4,
            "save_path": "/media/Movies",
            "content_path": None,
        }
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    status = asyncio.run(get_download_status_from_qbittorrent(_settings(), INFO_HASH))

    assert status is not None
    assert status.content_path is None


def test_add_download_tags_new_torrent_for_requester(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.hashes_after_add = [INFO_HASH]
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        add_download_to_qbittorrent(
            "http://prowlarr.test/1/download?link=abc",
            _settings(),
            requester_id="telegram:123456789",
        )
    )

    requester_tag = _requester_tag_for_user("telegram:123456789")
    assert FakeQbittorrentClient.calls == [
        {
            "torrent_files": TORRENT_CONTENT,
            "tags": f"qbitlarr.managed,{requester_tag}",
            "save_path": None,
        }
    ]
    assert FakeQbittorrentClient.tag_calls == [
        {
            "tags": f"qbitlarr.managed,{requester_tag}",
            "torrent_hashes": INFO_HASH,
        }
    ]


def test_add_download_tags_existing_torrent_for_new_requester(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        add_download_to_qbittorrent(
            "http://prowlarr.test/1/download?link=abc",
            _settings(),
            requester_id="telegram:123456789",
        )
    )

    requester_tag = _requester_tag_for_user("telegram:123456789")
    assert FakeQbittorrentClient.calls == []
    assert FakeQbittorrentClient.tag_calls == [
        {
            "tags": f"qbitlarr.managed,{requester_tag}",
            "torrent_hashes": INFO_HASH,
        }
    ]


def test_requester_tags_include_original_id_digest_to_prevent_sanitization_collisions():
    colon_tag = _requester_tag_for_user("telegram:123")
    dash_tag = _requester_tag_for_user("telegram-123")

    assert colon_tag != dash_tag
    assert colon_tag.startswith("requester.telegram-123-")
    assert dash_tag.startswith("requester.telegram-123-")
    assert len(_requester_tag_for_user("telegram:" + ("x" * 200))) <= 64


def test_add_download_applies_optional_retention_policy_to_new_torrent(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.hashes_after_add = [INFO_HASH]
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    settings = _settings()
    settings.retention_enabled = True
    settings.retention_ratio_limit = 2.0
    settings.retention_seeding_time_limit_minutes = 10080
    settings.retention_action = "Remove"

    asyncio.run(
        add_download_to_qbittorrent(
            "http://prowlarr.test/1/download?link=abc",
            settings,
        )
    )

    assert FakeQbittorrentClient.share_limit_calls == [
        {
            "ratio_limit": 2.0,
            "seeding_time_limit": 10080,
            "share_limit_action": "Remove",
            "torrent_hashes": INFO_HASH,
        }
    ]


def test_add_download_applies_optional_retention_policy_to_existing_torrent(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.httpx.AsyncClient", FakeAsyncClient)

    settings = _settings()
    settings.retention_enabled = True
    settings.retention_ratio_limit = 3.0
    settings.retention_seeding_time_limit_minutes = 1440
    settings.retention_action = "Remove"

    asyncio.run(
        add_download_to_qbittorrent(
            "http://prowlarr.test/1/download?link=abc",
            settings,
        )
    )

    assert FakeQbittorrentClient.calls == []
    assert FakeQbittorrentClient.share_limit_calls == [
        {
            "ratio_limit": 3.0,
            "seeding_time_limit": 1440,
            "share_limit_action": "Remove",
            "torrent_hashes": INFO_HASH,
        }
    ]


def test_list_downloads_filters_by_requester_tag(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH, "otherhash"]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        INFO_HASH: {_requester_tag_for_user("telegram:123456789")},
        "otherhash": {_requester_tag_for_user("telegram:12345")},
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    downloads = asyncio.run(list_downloads_from_qbittorrent(_settings(), requester_id="telegram:123456789"))

    assert [download.hash for download in downloads] == [INFO_HASH]


def test_get_download_status_respects_requester_tag_filter(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        INFO_HASH: {_requester_tag_for_user("telegram:123456789")},
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    status = asyncio.run(
        get_download_status_from_qbittorrent(
            _settings(),
            INFO_HASH,
            requester_id="telegram:99999",
        )
    )

    assert status is None


def test_pause_download_requires_matching_requester_tag(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        INFO_HASH: {_requester_tag_for_user("telegram:123456789")},
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    status = asyncio.run(
        pause_download_in_qbittorrent(
            _settings(),
            INFO_HASH,
            requester_id="telegram:123456789",
        )
    )

    assert status.hash == INFO_HASH
    assert status.state == "pausedDL"
    assert FakeQbittorrentClient.pause_calls == [{"torrent_hashes": INFO_HASH}]


def test_resume_download_requires_matching_requester_tag(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        INFO_HASH: {_requester_tag_for_user("telegram:123456789")},
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    FakeQbittorrentClient.torrent_overrides_by_hash = {
        INFO_HASH: {"state": "pausedDL"},
    }

    status = asyncio.run(
        resume_download_in_qbittorrent(
            _settings(),
            INFO_HASH,
            requester_id="telegram:123456789",
        )
    )

    assert status.hash == INFO_HASH
    assert status.state == "downloading"
    assert FakeQbittorrentClient.resume_calls == [{"torrent_hashes": INFO_HASH}]


def test_delete_download_requires_matching_requester_tag_and_keeps_files(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        INFO_HASH: {_requester_tag_for_user("telegram:123456789")},
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    status = asyncio.run(
        delete_download_from_qbittorrent(
            _settings(),
            INFO_HASH,
            requester_id="telegram:123456789",
        )
    )

    assert status.hash == INFO_HASH
    assert FakeQbittorrentClient.delete_calls == [
        {
            "delete_files": False,
            "torrent_hashes": INFO_HASH,
        }
    ]


@pytest.mark.parametrize(
    "control",
    [pause_download_in_qbittorrent, resume_download_in_qbittorrent, delete_download_from_qbittorrent],
)
def test_requester_cannot_control_download_shared_with_another_requester(monkeypatch, control):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        INFO_HASH: {
            _requester_tag_for_user("telegram:first"),
            _requester_tag_for_user("telegram:second"),
        },
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    with pytest.raises(SharedDownloadControlError, match="shared by multiple requesters"):
        asyncio.run(control(_settings(), INFO_HASH, requester_id="telegram:first"))

    assert FakeQbittorrentClient.pause_calls == []
    assert FakeQbittorrentClient.resume_calls == []
    assert FakeQbittorrentClient.delete_calls == []


def test_download_control_does_not_touch_torrent_for_wrong_requester(monkeypatch):
    _reset_fakes()
    FakeQbittorrentClient.existing_hashes = [INFO_HASH]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        INFO_HASH: {"requester.telegram-123456789"},
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    status = asyncio.run(
        pause_download_in_qbittorrent(
            _settings(),
            INFO_HASH,
            requester_id="telegram:99999",
        )
    )

    assert status is None
    assert FakeQbittorrentClient.pause_calls == []
    assert FakeQbittorrentClient.resume_calls == []
    assert FakeQbittorrentClient.delete_calls == []


def test_cleanup_completed_downloads_removes_only_old_qbitlarr_tasks_without_files(monkeypatch):
    _reset_fakes()
    now = 2_000_000
    old_completion = now - 259_200 - 1
    fresh_completion = now - 60
    FakeQbittorrentClient.existing_hashes = [
        "oldmanaged",
        "freshmanaged",
        "oldrequester",
        "oldmanual",
        "incomplete",
    ]
    FakeQbittorrentClient.torrent_tags_by_hash = {
        "oldmanaged": {"qbitlarr.managed"},
        "freshmanaged": {"qbitlarr.managed"},
        "oldrequester": {"requester.telegram-123456789"},
        "oldmanual": set(),
        "incomplete": {"qbitlarr.managed"},
    }
    FakeQbittorrentClient.torrent_overrides_by_hash = {
        "oldmanaged": {"progress": 1.0, "state": "uploading", "completion_on": old_completion},
        "freshmanaged": {"progress": 1.0, "state": "uploading", "completion_on": fresh_completion},
        "oldrequester": {"progress": 1.0, "state": "uploading", "completion_on": old_completion},
        "oldmanual": {"progress": 1.0, "state": "uploading", "completion_on": old_completion},
        "incomplete": {"progress": 0.9, "state": "downloading", "completion_on": -1},
    }
    monkeypatch.setattr("mpilot.acquisition.services.qbittorrent.qbittorrentapi.Client", FakeQbittorrentClient)

    settings = _settings()
    settings.cleanup_enabled = True
    summary = asyncio.run(cleanup_completed_downloads_from_qbittorrent(settings, now=now))

    assert summary == {
        "status": "success",
        "deleted_count": 2,
        "deleted_hashes": ["oldmanaged", "oldrequester"],
    }
    assert FakeQbittorrentClient.delete_calls == [
        {
            "delete_files": False,
            "torrent_hashes": "oldmanaged|oldrequester",
        }
    ]
