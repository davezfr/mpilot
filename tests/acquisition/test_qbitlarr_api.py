import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mpilot.api import main as app_main
from mpilot.api.download import _download_title_from_link
from mpilot.api.main import (
    DownloadRequest,
    HandleRequest,
    SearchRequest,
    Settings,
    _cleanup_completed_downloads_loop,
    app,
    build_prowlarr_search_params,
    normalize_download_link,
    normalize_search_results,
)
from mpilot.acquisition.models import TorrentStatus
from mpilot.acquisition.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def allow_unauthenticated_loopback_for_api_tests(monkeypatch):
    monkeypatch.setenv("MPILOT_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")
    monkeypatch.delenv("QBITLARR_REQUESTER_API_KEYS", raising=False)
    monkeypatch.delenv("MPILOT_ACQUISITION_REQUESTER_API_KEYS", raising=False)


def test_settings_defaults_public_save_paths(monkeypatch):
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.delenv("PROWLARR_DOWNLOAD_URL", raising=False)
    monkeypatch.setenv("PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("QBIT_URL", "http://host.docker.internal:8080")
    monkeypatch.setenv("QBIT_USERNAME", "qbit-user")
    monkeypatch.setenv("QBIT_PASSWORD", "qbit-pass")
    monkeypatch.delenv("QBITLARR_SAVE_PATH_MOVIE", raising=False)
    monkeypatch.delenv("QBITLARR_SAVE_PATH_MOVIE_4K", raising=False)
    monkeypatch.delenv("QBITLARR_SAVE_PATH_TV", raising=False)
    monkeypatch.delenv("QBITLARR_EXTRA_SAVE_PATHS", raising=False)
    monkeypatch.delenv("QBITLARR_MANUAL_RESULT_LIMIT", raising=False)
    monkeypatch.delenv("QBITLARR_CHOICE_STYLE", raising=False)

    settings = Settings.from_env()

    assert settings.qbitlarr_save_path_movie == "/downloads/movies"
    assert settings.qbitlarr_save_path_movie_4k == "/downloads/movies-4k"
    assert settings.qbitlarr_save_path_tv == "/downloads/tv"
    assert settings.qbitlarr_extra_save_paths is None
    assert settings.manual_result_limit == 4
    assert settings.choice_style == "hermes-default"


def test_settings_accepts_custom_save_paths(monkeypatch):
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("QBIT_URL", "http://host.docker.internal:8080")
    monkeypatch.setenv("QBIT_USERNAME", "qbit-user")
    monkeypatch.setenv("QBIT_PASSWORD", "qbit-pass")
    monkeypatch.setenv("QBITLARR_SAVE_PATH_MOVIE", "/media/Movies")
    monkeypatch.setenv("QBITLARR_SAVE_PATH_MOVIE_4K", "/media/Movies 4K")
    monkeypatch.setenv("QBITLARR_SAVE_PATH_TV", "/media/TV")
    monkeypatch.setenv("QBITLARR_EXTRA_SAVE_PATHS", "/media/Kids,/media/Documentaries")

    settings = Settings.from_env()

    assert settings.qbitlarr_save_path_movie == "/media/Movies"
    assert settings.qbitlarr_save_path_movie_4k == "/media/Movies 4K"
    assert settings.qbitlarr_save_path_tv == "/media/TV"
    assert settings.qbitlarr_extra_save_paths == ["/media/Kids", "/media/Documentaries"]


def test_settings_prefers_mpilot_acquisition_env_aliases(monkeypatch):
    monkeypatch.setenv("MPILOT_PROWLARR_URL", "http://mpilot-prowlarr:9696")
    monkeypatch.setenv("MPILOT_PROWLARR_API_KEY", "mpilot-prowlarr-key")
    monkeypatch.setenv("MPILOT_QBIT_URL", "http://mpilot-qbit:8080")
    monkeypatch.setenv("MPILOT_QBIT_USERNAME", "mpilot-user")
    monkeypatch.setenv("MPILOT_QBIT_PASSWORD", "mpilot-pass")
    monkeypatch.setenv("MPILOT_ACQUISITION_SAVE_PATH_MOVIE", "/media/Movies")
    monkeypatch.setenv("MPILOT_ACQUISITION_SAVE_PATH_MOVIE_4K", "/media/Movies 4K")
    monkeypatch.setenv("MPILOT_ACQUISITION_SAVE_PATH_TV", "/media/TV")
    monkeypatch.setenv("MPILOT_ACQUISITION_MANUAL_RESULT_LIMIT", "6")
    monkeypatch.setenv("PROWLARR_URL", "http://legacy-prowlarr:9696")
    monkeypatch.setenv("QBITLARR_SAVE_PATH_MOVIE", "/legacy/Movies")

    settings = Settings.from_env()

    assert settings.prowlarr_url == "http://mpilot-prowlarr:9696"
    assert settings.prowlarr_api_key == "mpilot-prowlarr-key"
    assert settings.qbit_url == "http://mpilot-qbit:8080"
    assert settings.qbit_username == "mpilot-user"
    assert settings.qbit_password == "mpilot-pass"
    assert settings.qbitlarr_save_path_movie == "/media/Movies"
    assert settings.qbitlarr_save_path_movie_4k == "/media/Movies 4K"
    assert settings.qbitlarr_save_path_tv == "/media/TV"
    assert settings.manual_result_limit == 6


def test_settings_loads_imdb_indexer_search_mode_lists(monkeypatch):
    monkeypatch.setenv("MPILOT_PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("MPILOT_PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("MPILOT_QBIT_URL", "http://qbit:8080")
    monkeypatch.setenv("MPILOT_QBIT_USERNAME", "user")
    monkeypatch.setenv("MPILOT_QBIT_PASSWORD", "pass")
    monkeypatch.setenv("MPILOT_PROWLARR_IMDB_NATIVE_INDEXER_IDS", "5,6")
    monkeypatch.setenv("MPILOT_PROWLARR_IMDB_KEYWORD_INDEXER_IDS", "4")
    monkeypatch.setenv("MPILOT_PROWLARR_IMDB_DISABLED_INDEXER_IDS", "1,3")

    settings = Settings.from_env()

    assert settings.prowlarr_imdb_native_indexer_ids == [5, 6]
    assert settings.prowlarr_imdb_keyword_indexer_ids == [4]
    assert settings.prowlarr_imdb_disabled_indexer_ids == [1, 3]
    assert settings.imdb_indexer_routing_configured is True


def test_settings_rejects_indexer_in_multiple_imdb_search_modes(monkeypatch):
    monkeypatch.setenv("MPILOT_PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("MPILOT_PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("MPILOT_QBIT_URL", "http://qbit:8080")
    monkeypatch.setenv("MPILOT_QBIT_USERNAME", "user")
    monkeypatch.setenv("MPILOT_QBIT_PASSWORD", "pass")
    monkeypatch.setenv("MPILOT_PROWLARR_IMDB_NATIVE_INDEXER_IDS", "5,6")
    monkeypatch.setenv("MPILOT_PROWLARR_IMDB_KEYWORD_INDEXER_IDS", "6")

    with pytest.raises(ConfigurationError, match="only one search mode: 6"):
        Settings.from_env()


def test_settings_accepts_custom_choice_display(monkeypatch):
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("QBIT_URL", "http://host.docker.internal:8080")
    monkeypatch.setenv("QBIT_USERNAME", "qbit-user")
    monkeypatch.setenv("QBIT_PASSWORD", "qbit-pass")
    monkeypatch.setenv("QBITLARR_MANUAL_RESULT_LIMIT", "5")
    monkeypatch.setenv("QBITLARR_CHOICE_STYLE", "telegram-rich")

    settings = Settings.from_env()

    assert settings.manual_result_limit == 5
    assert settings.choice_style == "telegram-rich"


def test_settings_blank_choice_limit_uses_default(monkeypatch):
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("QBIT_URL", "http://host.docker.internal:8080")
    monkeypatch.setenv("QBIT_USERNAME", "qbit-user")
    monkeypatch.setenv("QBIT_PASSWORD", "qbit-pass")
    monkeypatch.setenv("QBITLARR_MANUAL_RESULT_LIMIT", "")

    settings = Settings.from_env()

    assert settings.manual_result_limit == 4


def test_settings_retention_policy_defaults_to_disabled(monkeypatch):
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("QBIT_URL", "http://host.docker.internal:8080")
    monkeypatch.setenv("QBIT_USERNAME", "qbit-user")
    monkeypatch.setenv("QBIT_PASSWORD", "qbit-pass")
    monkeypatch.delenv("QBITLARR_RETENTION_ENABLED", raising=False)
    monkeypatch.delenv("QBITLARR_RETENTION_RATIO_LIMIT", raising=False)
    monkeypatch.delenv("QBITLARR_RETENTION_SEEDING_TIME_LIMIT_MINUTES", raising=False)
    monkeypatch.delenv("QBITLARR_RETENTION_ACTION", raising=False)

    settings = Settings.from_env()

    assert settings.retention_enabled is False
    assert settings.retention_ratio_limit == 2.0
    assert settings.retention_seeding_time_limit_minutes == 10080
    assert settings.retention_action == "Remove"
    assert settings.cleanup_enabled is False
    assert settings.cleanup_completed_after_seconds == 259_200
    assert settings.cleanup_interval_seconds == 21_600
    assert settings.cleanup_include_legacy_requester_tags is True


def test_settings_accepts_custom_retention_policy(monkeypatch):
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "prowlarr-key")
    monkeypatch.setenv("QBIT_URL", "http://host.docker.internal:8080")
    monkeypatch.setenv("QBIT_USERNAME", "qbit-user")
    monkeypatch.setenv("QBIT_PASSWORD", "qbit-pass")
    monkeypatch.setenv("QBITLARR_RETENTION_ENABLED", "true")
    monkeypatch.setenv("QBITLARR_RETENTION_RATIO_LIMIT", "1.5")
    monkeypatch.setenv("QBITLARR_RETENTION_SEEDING_TIME_LIMIT_MINUTES", "4320")
    monkeypatch.setenv("QBITLARR_RETENTION_ACTION", "remove")
    monkeypatch.setenv("QBITLARR_CLEANUP_ENABLED", "true")
    monkeypatch.setenv("QBITLARR_CLEANUP_COMPLETED_AFTER_SECONDS", "172800")
    monkeypatch.setenv("QBITLARR_CLEANUP_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("QBITLARR_CLEANUP_INCLUDE_LEGACY_REQUESTER_TAGS", "false")

    settings = Settings.from_env()

    assert settings.retention_enabled is True
    assert settings.retention_ratio_limit == 1.5
    assert settings.retention_seeding_time_limit_minutes == 4320
    assert settings.retention_action == "Remove"
    assert settings.cleanup_enabled is True
    assert settings.cleanup_completed_after_seconds == 172800
    assert settings.cleanup_interval_seconds == 3600
    assert settings.cleanup_include_legacy_requester_tags is False


def test_start_cleanup_task_starts_loop_when_download_cleanup_is_disabled(monkeypatch):
    settings = SimpleNamespace(cleanup_enabled=False, cleanup_interval_seconds=120)
    created = []

    class FakeTask:
        def done(self):
            return False

    fake_task = FakeTask()

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return fake_task

    monkeypatch.setattr(app_main, "_cleanup_task", None)
    monkeypatch.setattr(app_main, "get_settings", lambda: settings)
    monkeypatch.setattr(app_main.asyncio, "create_task", fake_create_task)

    asyncio.run(app_main.start_cleanup_task())

    assert len(created) == 1
    assert app_main._cleanup_task is fake_task


def test_app_lifespan_starts_and_stops_cleanup_task(monkeypatch):
    events = []

    async def fake_start_cleanup_task():
        events.append("startup")

    async def fake_stop_cleanup_task():
        events.append("shutdown")

    monkeypatch.setattr(app_main, "start_cleanup_task", fake_start_cleanup_task)
    monkeypatch.setattr(app_main, "stop_cleanup_task", fake_stop_cleanup_task)

    with TestClient(app_main.app):
        assert events == ["startup"]

    assert events == ["startup", "shutdown"]


def test_cleanup_loop_runs_once_before_sleeping():
    settings = SimpleNamespace(cleanup_enabled=True, cleanup_interval_seconds=120)
    cleanup_calls = []
    sleep_calls = []

    async def fake_cleanup(arg):
        cleanup_calls.append(arg)
        return {"status": "success", "deleted_count": 0, "deleted_hashes": []}

    async def fake_prune(_arg):
        return {"status": "success", "deleted_count": 0}

    async def fake_sleep(interval):
        sleep_calls.append(interval)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _cleanup_completed_downloads_loop(
                settings,
                cleanup_func=fake_cleanup,
                snapshot_prune_func=fake_prune,
                sleep_func=fake_sleep,
            )
        )

    assert cleanup_calls == [settings]
    assert sleep_calls == [120.0]


def test_cleanup_loop_continues_after_unexpected_error():
    settings = SimpleNamespace(cleanup_enabled=True, cleanup_interval_seconds=120)
    cleanup_calls = []
    sleep_calls = []

    async def fake_cleanup(arg):
        cleanup_calls.append(arg)
        if len(cleanup_calls) == 1:
            raise TypeError("unexpected qBittorrent shape")
        return {"status": "success", "deleted_count": 0, "deleted_hashes": []}

    async def fake_prune(_arg):
        return {"status": "success", "deleted_count": 0}

    async def fake_sleep(interval):
        sleep_calls.append(interval)
        if len(sleep_calls) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _cleanup_completed_downloads_loop(
                settings,
                cleanup_func=fake_cleanup,
                snapshot_prune_func=fake_prune,
                sleep_func=fake_sleep,
            )
        )

    assert cleanup_calls == [settings, settings]
    assert sleep_calls == [120.0, 120.0]


def test_cleanup_loop_prunes_query_snapshots_after_download_cleanup():
    settings = SimpleNamespace(cleanup_enabled=True, cleanup_interval_seconds=120)
    cleanup_calls = []
    prune_calls = []
    sleep_calls = []

    async def fake_cleanup(arg):
        cleanup_calls.append(arg)
        return {"status": "success", "deleted_count": 0, "deleted_hashes": []}

    async def fake_prune(arg):
        prune_calls.append(arg)
        return {"status": "success", "deleted_count": 2}

    async def fake_sleep(interval):
        sleep_calls.append(interval)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _cleanup_completed_downloads_loop(
                settings,
                cleanup_func=fake_cleanup,
                snapshot_prune_func=fake_prune,
                sleep_func=fake_sleep,
            )
        )

    assert cleanup_calls == [settings]
    assert prune_calls == [settings]
    assert sleep_calls == [120.0]


def test_cleanup_loop_skips_download_cleanup_when_disabled_but_prunes_query_snapshots():
    settings = SimpleNamespace(cleanup_enabled=False, cleanup_interval_seconds=120)
    cleanup_calls = []
    prune_calls = []
    sleep_calls = []

    async def fake_cleanup(arg):
        cleanup_calls.append(arg)
        raise AssertionError("download cleanup should be disabled")

    async def fake_prune(arg):
        prune_calls.append(arg)
        return {"status": "success", "deleted_count": 1}

    async def fake_sleep(interval):
        sleep_calls.append(interval)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _cleanup_completed_downloads_loop(
                settings,
                cleanup_func=fake_cleanup,
                snapshot_prune_func=fake_prune,
                sleep_func=fake_sleep,
            )
        )

    assert cleanup_calls == []
    assert prune_calls == [settings]
    assert sleep_calls == [120.0]


def test_cleanup_loop_prunes_query_snapshots_after_download_cleanup_failure():
    settings = SimpleNamespace(cleanup_enabled=True, cleanup_interval_seconds=120)
    cleanup_calls = []
    prune_calls = []
    sleep_calls = []

    async def fake_cleanup(arg):
        cleanup_calls.append(arg)
        raise TypeError("unexpected qBittorrent shape")

    async def fake_prune(arg):
        prune_calls.append(arg)
        return {"status": "success", "deleted_count": 1}

    async def fake_sleep(interval):
        sleep_calls.append(interval)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _cleanup_completed_downloads_loop(
                settings,
                cleanup_func=fake_cleanup,
                snapshot_prune_func=fake_prune,
                sleep_func=fake_sleep,
            )
        )

    assert cleanup_calls == [settings]
    assert prune_calls == [settings]
    assert sleep_calls == [120.0]


def test_query_snapshot_store_prunes_old_snapshots(tmp_path):
    from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore

    store = QuerySnapshotStore(str(tmp_path))
    store.create(
        query_id="old-query",
        request={"input": "old"},
        status="primary_ready",
        reason="primary_results_ready",
        results=[],
    )
    store.create(
        query_id="new-query",
        request={"input": "new"},
        status="primary_ready",
        reason="primary_results_ready",
        results=[],
    )
    old_path = tmp_path / "old-query.json"
    payload = json.loads(old_path.read_text(encoding="utf-8"))
    payload["updated_at"] = "2026-01-01T00:00:00Z"
    old_path.write_text(json.dumps(payload), encoding="utf-8")

    summary = store.prune(
        now=datetime(2026, 1, 10, tzinfo=UTC),
        retention=timedelta(days=7),
    )

    assert summary["deleted_count"] == 1
    assert summary["deleted_query_ids"] == ["old-query"]
    assert not old_path.exists()
    assert (tmp_path / "new-query.json").exists()


def test_query_snapshot_store_prune_skips_invalid_timestamp_and_prunes_other_snapshots(tmp_path):
    from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore

    store = QuerySnapshotStore(str(tmp_path))
    store.create(
        query_id="old-query",
        request={"input": "old"},
        status="primary_ready",
        reason="primary_results_ready",
        results=[],
    )
    store.create(
        query_id="new-query",
        request={"input": "new"},
        status="primary_ready",
        reason="primary_results_ready",
        results=[],
    )
    old_path = tmp_path / "old-query.json"
    old_payload = json.loads(old_path.read_text(encoding="utf-8"))
    old_payload["updated_at"] = "2026-01-01T00:00:00Z"
    old_path.write_text(json.dumps(old_payload), encoding="utf-8")

    bad_timestamp_payload = dict(old_payload)
    bad_timestamp_payload["query_id"] = "bad-timestamp-query"
    bad_timestamp_payload["updated_at"] = "not-a-timestamp"
    bad_timestamp_path = tmp_path / "bad-timestamp-query.json"
    bad_timestamp_path.write_text(json.dumps(bad_timestamp_payload), encoding="utf-8")

    summary = store.prune(
        now=datetime(2026, 1, 10, tzinfo=UTC),
        retention=timedelta(days=7),
    )

    assert summary["deleted_count"] == 1
    assert summary["deleted_query_ids"] == ["old-query"]
    assert not old_path.exists()
    assert bad_timestamp_path.exists()
    assert (tmp_path / "new-query.json").exists()


def test_build_prowlarr_search_params_uses_query_search_by_default():
    request = SearchRequest(query="ubuntu 24.04")

    params = build_prowlarr_search_params(request)

    assert params["query"] == "ubuntu 24.04"
    assert params["type"] == "search"
    assert params["limit"] == 50
    assert params["offset"] == 0


def test_build_prowlarr_search_params_includes_categories_when_provided():
    request = SearchRequest(query="The Hitch-Hiker", categories=[2040])

    params = build_prowlarr_search_params(request)

    assert params["categories"] == [2040]


def test_build_prowlarr_search_params_accepts_structured_search_type():
    request = SearchRequest(query="{ImdbId:tt0045877}", categories=[2000])

    params = build_prowlarr_search_params(request, search_type="movie")

    assert params["query"] == "{ImdbId:tt0045877}"
    assert params["type"] == "movie"


def test_build_prowlarr_search_params_includes_indexer_ids_when_provided():
    request = SearchRequest(query="The Hitch-Hiker", indexer_ids=[10, 20])

    params = build_prowlarr_search_params(request)

    assert params["indexerIds"] == [10, 20]


def test_build_prowlarr_search_params_converts_known_identifiers():
    request = SearchRequest(identifier="imdb:tt0045877", query="season 2")

    params = build_prowlarr_search_params(request)

    assert params["query"] == "tt0045877 season 2"


def test_build_prowlarr_search_params_converts_imdb_url_identifier():
    request = SearchRequest(identifier="https://www.imdb.com/title/tt0045877/?ref_=ext_shr_lnk")

    params = build_prowlarr_search_params(request)

    assert params["query"] == "tt0045877"


def test_build_prowlarr_search_params_converts_imdb_url_query():
    request = SearchRequest(query="https://m.imdb.com/title/tt0045877/?utm_source=whatsapp")

    params = build_prowlarr_search_params(request)

    assert params["query"] == "tt0045877"


def test_normalize_search_results_filters_duplicates_and_invalid_links():
    raw_results = [
        {
            "title": "First",
            "downloadUrl": "/api/v1/indexer/1/download?link=abc",
            "size": 100,
            "seeders": 10,
            "leechers": 2,
            "indexer": "Indexer A",
        },
        {
            "title": "Duplicate",
            "downloadUrl": "/api/v1/indexer/1/download?link=abc",
            "size": 100,
            "seeders": 9,
            "leechers": 1,
            "indexer": "Indexer B",
        },
        {"title": "Missing Link", "size": 50, "indexer": "Indexer C"},
        {
            "title": "Magnet",
            "magnetUrl": "magnet:?xt=urn:btih:abcdef",
            "size": 200,
            "seeders": 5,
            "leechers": 0,
            "indexer": "Indexer D",
        },
    ]
    raw_results.extend(
        {
            "title": f"Extra {index}",
            "downloadUrl": f"https://example.test/{index}.torrent",
            "size": index,
            "indexer": "Indexer E",
        }
        for index in range(25)
    )

    normalized = normalize_search_results(
        raw_results,
        prowlarr_url="http://prowlarr:9696",
        prowlarr_api_key="secret",
    )

    assert len(normalized) == 27
    assert normalized[0].title == "First"
    assert normalized[0].download_link == "http://prowlarr:9696/api/v1/indexer/1/download?link=abc"
    assert normalized[1].download_link == "magnet:?xt=urn:btih:abcdef"
    assert len({item.download_link for item in normalized}) == len(normalized)


def test_normalize_search_results_rewrites_prowlarr_download_links_to_download_base():
    raw_results = [
        {
            "title": "Night of the Living Dead",
            "downloadUrl": "/4/download?link=abc&file=Night+of+the+Living+Dead",
            "size": 100,
            "seeders": 10,
        },
        {
            "title": "The Hitch-Hiker",
            "downloadUrl": "http://prowlarr:9696/4/download?link=def&file=The+Hitch-Hiker",
            "size": 100,
            "seeders": 10,
        },
    ]

    normalized = normalize_search_results(
        raw_results,
        prowlarr_url="http://prowlarr:9696",
        prowlarr_download_url="http://192.0.2.10:9696",
        prowlarr_api_key="secret",
    )

    assert normalized[0].download_link == "http://192.0.2.10:9696/4/download?link=abc&file=Night+of+the+Living+Dead"
    assert normalized[1].download_link == "http://192.0.2.10:9696/4/download?link=def&file=The+Hitch-Hiker"


def test_normalize_search_results_prefers_actual_magnet_over_prowlarr_proxy_fields():
    raw_results = [
        {
            "title": "Night of the Living Dead",
            "magnetUrl": "http://prowlarr:9696/4/download?link=abc&file=Night+of+the+Living+Dead",
            "guid": "magnet:?xt=urn:btih:abcdef",
            "size": 100,
            "seeders": 10,
        }
    ]

    normalized = normalize_search_results(
        raw_results,
        prowlarr_url="http://prowlarr:9696",
        prowlarr_download_url="http://192.0.2.10:9696",
        prowlarr_api_key="secret",
    )

    assert normalized[0].download_link == "magnet:?xt=urn:btih:abcdef"


def test_normalize_search_results_prefers_download_url_over_html_guid():
    raw_results = [
        {
            "title": "Within Our Gates",
            "downloadUrl": "http://prowlarr:9696/1/download?link=abc&file=Within+Our+Gates",
            "guid": "https://example.test/torrent/within-our-gates",
            "size": 100,
            "seeders": 10,
        }
    ]

    normalized = normalize_search_results(
        raw_results,
        prowlarr_url="http://prowlarr:9696",
        prowlarr_download_url="http://192.0.2.10:9696",
        prowlarr_api_key="secret",
    )

    assert normalized[0].download_link == "http://192.0.2.10:9696/1/download?link=abc&file=Within+Our+Gates"


def test_download_request_accepts_http_https_magnet_and_rejects_other_schemes():
    assert DownloadRequest(download_link="https://example.test/file.torrent")
    assert DownloadRequest(download_link="magnet:?xt=urn:btih:abcdef")

    with pytest.raises(ValueError):
        DownloadRequest(download_link="file:///etc/passwd")


def test_download_request_accepts_optional_save_path():
    request = DownloadRequest(
        download_link="magnet:?xt=urn:btih:abcdef",
        save_path="  /media/Kids  ",
    )

    assert request.save_path == "/media/Kids"


def test_download_request_accepts_optional_user_id():
    request = DownloadRequest(
        download_link="magnet:?xt=urn:btih:abcdef",
        user_id="  telegram:123456789  ",
    )

    assert request.user_id == "telegram:123456789"


def test_handle_request_accepts_optional_save_path():
    request = HandleRequest(user_message="tt0045877", save_path="  /media/Kids  ")

    assert request.save_path == "/media/Kids"


def test_normalize_download_link_rejects_blank_links():
    with pytest.raises(ValueError):
        normalize_download_link("   ")


def test_download_endpoint_passes_save_path_to_qbittorrent(monkeypatch, tmp_path):
    queued: dict[str, str | None] = {}

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path
        queued["requester_id"] = requester_id
        return TorrentStatus(
            name="Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            state="downloading",
            progress=0.25,
            size=1_000_000_000,
            seeds=10,
            hash="abcdef1234567890",
        )

    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr(
        "mpilot.api.download.get_settings",
        lambda: SimpleNamespace(
            qbitlarr_save_path_movie="/downloads/movies",
            qbitlarr_save_path_movie_4k="/downloads/movies-4k",
            qbitlarr_save_path_tv="/downloads/tv",
            qbitlarr_extra_save_paths=["/media/Kids"],
        ),
    )

    client = TestClient(app)
    response = client.post(
        "/download",
        json={
            "download_link": "magnet:?xt=urn:btih:abcdef",
            "save_path": "/media/Kids",
            "user_id": "telegram:123456789",
        },
    )

    assert response.status_code == 200
    assert response.json()["download_status"]["hash"] == "abcdef1234567890"
    assert queued == {
        "download_link": "magnet:?xt=urn:btih:abcdef",
        "save_path": "/media/Kids",
        "requester_id": "telegram:123456789",
    }


def test_download_endpoint_infers_movie_save_path_when_save_path_is_omitted(monkeypatch):
    queued: dict[str, str | None] = {}

    async def fake_download_title_from_link(download_link, settings):
        return "The Hitch-Hiker 1953 1080p AMZN WEB-DL DDP2 0 H 264-GPRS"

    async def fake_add_download(download_link, settings, *, save_path=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.download._download_title_from_link", fake_download_title_from_link)
    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr(
        "mpilot.api.download.get_settings",
        lambda: SimpleNamespace(
            qbitlarr_save_path_movie="/downloads/movies",
            qbitlarr_save_path_movie_4k="/downloads/movies-4k",
            qbitlarr_save_path_tv="/downloads/tv",
            qbitlarr_extra_save_paths=None,
        ),
    )

    client = TestClient(app)
    response = client.post(
        "/download",
        json={"download_link": "http://prowlarr.test/1/download"},
    )

    assert response.status_code == 200
    assert queued == {
        "download_link": "http://prowlarr.test/1/download",
        "save_path": "/downloads/movies",
    }


def test_download_endpoint_infers_4k_movie_save_path_when_save_path_is_omitted(monkeypatch):
    queued: dict[str, str | None] = {}

    async def fake_download_title_from_link(download_link, settings):
        return "The Hitch-Hiker 1953 2160p UHD BluRay x265"

    async def fake_add_download(download_link, settings, *, save_path=None):
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.download._download_title_from_link", fake_download_title_from_link)
    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr(
        "mpilot.api.download.get_settings",
        lambda: SimpleNamespace(
            qbitlarr_save_path_movie="/downloads/movies",
            qbitlarr_save_path_movie_4k="/downloads/movies-4k",
            qbitlarr_save_path_tv="/downloads/tv",
            qbitlarr_extra_save_paths=None,
        ),
    )

    client = TestClient(app)
    response = client.post(
        "/download",
        json={"download_link": "http://prowlarr.test/1/download"},
    )

    assert response.status_code == 200
    assert queued["save_path"] == "/downloads/movies-4k"


def test_download_endpoint_infers_tv_save_path_when_save_path_is_omitted(monkeypatch):
    queued: dict[str, str | None] = {}

    async def fake_download_title_from_link(download_link, settings):
        return "Example Show S01E01 1080p WEB-DL H264"

    async def fake_add_download(download_link, settings, *, save_path=None):
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.download._download_title_from_link", fake_download_title_from_link)
    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr(
        "mpilot.api.download.get_settings",
        lambda: SimpleNamespace(
            qbitlarr_save_path_movie="/downloads/movies",
            qbitlarr_save_path_movie_4k="/downloads/movies-4k",
            qbitlarr_save_path_tv="/downloads/tv",
            qbitlarr_extra_save_paths=None,
        ),
    )

    client = TestClient(app)
    response = client.post(
        "/download",
        json={"download_link": "magnet:?xt=urn:btih:abcdef"},
    )

    assert response.status_code == 200
    assert queued["save_path"] == "/downloads/tv/Example Show"


def test_download_endpoint_uses_query_context_to_keep_manual_selection_in_tv_path(monkeypatch, tmp_path):
    import mpilot.api.download as download_api

    queued: dict[str, str | None] = {}

    async def fake_download_title_from_link(download_link, settings):
        return "Example Show 2026 1080p WEB-DL H264"

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path
        queued["requester_id"] = requester_id

    monkeypatch.setattr("mpilot.api.download._download_title_from_link", fake_download_title_from_link)
    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr(
        download_api,
        "get_settings",
        lambda: SimpleNamespace(
            query_snapshot_dir=str(tmp_path),
            qbitlarr_save_path_movie="/downloads/movies",
            qbitlarr_save_path_movie_4k="/downloads/movies-4k",
            qbitlarr_save_path_tv="/downloads/tv",
            qbitlarr_extra_save_paths=None,
        ),
    )

    from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore

    QuerySnapshotStore(str(tmp_path)).create(
        query_id="query-tv-manual",
        request={"input": "tt0017925", "query": "tt0017925", "requester_id": "telegram:123456789"},
        status="primary_ready",
        reason="primary_results_ready",
        results=[
            {
                "title": "Example.Show.S01.1080p.WEB-DL.H264",
                "download_link": "magnet:?xt=urn:btih:abcdef",
                "size": 1_000_000_000,
                "seeders": 10,
                "indexer": "Indexer A",
            }
        ],
    )

    client = TestClient(app)
    response = client.post(
        "/download",
        json={
            "download_link": "magnet:?xt=urn:btih:abcdef",
            "query_id": "query-tv-manual",
            "user_id": "telegram:123456789",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["imdb_id"] == "tt0017925"
    assert payload["media_type"] == "tv"
    assert queued == {
        "download_link": "magnet:?xt=urn:btih:abcdef",
        "save_path": "/downloads/tv/Example Show",
        "requester_id": "telegram:123456789",
    }


def test_download_endpoint_sanitizes_inferred_tv_show_folder(monkeypatch):
    queued: dict[str, str | None] = {}

    async def fake_download_title_from_link(download_link, settings):
        return "Example/Show S01E01 1080p WEB-DL H264"

    async def fake_add_download(download_link, settings, *, save_path=None):
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.download._download_title_from_link", fake_download_title_from_link)
    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr(
        "mpilot.api.download.get_settings",
        lambda: SimpleNamespace(
            qbitlarr_save_path_movie="/downloads/movies",
            qbitlarr_save_path_movie_4k="/downloads/movies-4k",
            qbitlarr_save_path_tv="/downloads/tv",
            qbitlarr_extra_save_paths=None,
        ),
    )

    client = TestClient(app)
    response = client.post(
        "/download",
        json={"download_link": "magnet:?xt=urn:btih:abcdef"},
    )

    assert response.status_code == 200
    assert queued["save_path"] == "/downloads/tv/Example Show"


def test_download_title_from_link_reads_magnet_display_name():
    title = asyncio.run(
        _download_title_from_link(
            "magnet:?xt=urn:btih:abcdef&dn=Example%20Show%20S01E01%201080p",
            SimpleNamespace(),
        )
    )

    assert title == "Example Show S01E01 1080p"


def test_download_endpoint_rejects_save_path_outside_allowed_roots(monkeypatch):
    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        raise AssertionError("download should not be queued")

    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr(
        "mpilot.api.download.get_settings",
        lambda: SimpleNamespace(
            qbitlarr_save_path_movie="/downloads/movies",
            qbitlarr_save_path_movie_4k="/downloads/movies-4k",
            qbitlarr_save_path_tv="/downloads/tv",
            qbitlarr_extra_save_paths=None,
        ),
    )

    client = TestClient(app)
    response = client.post(
        "/download",
        json={
            "download_link": "magnet:?xt=urn:btih:abcdef",
            "save_path": "/media/Kids",
        },
    )

    assert response.status_code == 400


def test_api_key_auth_blocks_requests_without_matching_header(monkeypatch):
    monkeypatch.setenv("QBITLARR_API_KEY", "secret-key")

    client = TestClient(app)

    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/health", headers={"X-API-Key": "secret-key"}).status_code == 200


def test_api_key_auth_rejects_empty_key_by_default(monkeypatch):
    monkeypatch.delenv("QBITLARR_API_KEY", raising=False)
    monkeypatch.delenv("MPILOT_ACQUISITION_API_KEY", raising=False)
    monkeypatch.delenv("MPILOT_ALLOW_UNAUTHENTICATED_LOOPBACK", raising=False)

    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 401
    assert response.json()["detail"] == "MPilot acquisition API key is required"


def test_requester_api_key_binds_omitted_user_filter_to_authenticated_requester(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    seen = {}

    async def fake_list_downloads(settings, requester_id=None):
        seen["requester_id"] = requester_id
        return []

    monkeypatch.delenv("QBITLARR_API_KEY", raising=False)
    monkeypatch.delenv("MPILOT_ACQUISITION_API_KEY", raising=False)
    monkeypatch.setenv(
        "MPILOT_ACQUISITION_REQUESTER_API_KEYS",
        json.dumps({"telegram:123": "requester-secret"}),
    )
    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "list_downloads_from_qbittorrent", fake_list_downloads)

    response = TestClient(app).get("/downloads", headers={"X-API-Key": "requester-secret"})

    assert response.status_code == 200
    assert seen["requester_id"] == "telegram:123"


def test_requester_api_key_rejects_spoofed_user_filter(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    monkeypatch.delenv("QBITLARR_API_KEY", raising=False)
    monkeypatch.delenv("MPILOT_ACQUISITION_API_KEY", raising=False)
    monkeypatch.setenv(
        "MPILOT_ACQUISITION_REQUESTER_API_KEYS",
        json.dumps({"telegram:123": "requester-secret"}),
    )
    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())

    response = TestClient(app).get(
        "/downloads?user_id=telegram:999",
        headers={"X-API-Key": "requester-secret"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Requester identity does not match API key"


def test_requester_api_key_cannot_equal_administrator_key(monkeypatch):
    monkeypatch.setenv("MPILOT_ACQUISITION_API_KEY", "shared-secret")
    monkeypatch.setenv("MPILOT_ACQUISITION_REQUESTER_API_KEYS", '{"telegram:123":"shared-secret"}')

    response = TestClient(app).get("/health", headers={"X-API-Key": "shared-secret"})

    assert response.status_code == 500
    assert "must differ from the administrator API key" in response.json()["detail"]


def test_deep_health_reports_dependency_status(monkeypatch):
    async def fake_prowlarr_health(settings):
        return {"status": "ok"}

    async def fake_qbittorrent_health(settings):
        return {"status": "ok"}

    monkeypatch.setattr("mpilot.api.main.get_settings", lambda: object())
    monkeypatch.setattr("mpilot.api.main.check_prowlarr_health", fake_prowlarr_health, raising=False)
    monkeypatch.setattr("mpilot.api.main.check_qbittorrent_health", fake_qbittorrent_health, raising=False)

    client = TestClient(app)
    response = client.get("/health?deep=true")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "MPilot Acquisition API",
        "dependencies": {
            "prowlarr": {"status": "ok"},
            "qbittorrent": {"status": "ok"},
        },
    }


def test_deep_health_returns_503_when_dependency_fails(monkeypatch):
    async def fake_prowlarr_health(settings):
        return {"status": "error", "detail": "Prowlarr is unreachable"}

    async def fake_qbittorrent_health(settings):
        return {"status": "ok"}

    monkeypatch.setattr("mpilot.api.main.get_settings", lambda: object())
    monkeypatch.setattr("mpilot.api.main.check_prowlarr_health", fake_prowlarr_health, raising=False)
    monkeypatch.setattr("mpilot.api.main.check_qbittorrent_health", fake_qbittorrent_health, raising=False)

    client = TestClient(app)
    response = client.get("/health?deep=true")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["dependencies"]["prowlarr"]["detail"] == "Prowlarr is unreachable"


def test_prowlarr_indexers_endpoint_returns_discoverable_indexer_ids(monkeypatch):
    import mpilot.api.prowlarr as prowlarr_api

    async def fake_list_indexers(settings):
        return [
            {
                "id": 10,
                "name": "Trusted Indexer",
                "enabled": True,
                "protocol": "torrent",
            }
        ]

    monkeypatch.setattr(prowlarr_api, "get_settings", lambda: object())
    monkeypatch.setattr(prowlarr_api, "list_prowlarr_indexers", fake_list_indexers)

    client = TestClient(app)
    response = client.get("/prowlarr/indexers")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 10,
            "name": "Trusted Indexer",
            "enabled": True,
            "protocol": "torrent",
            "supports_imdb_parameter": False,
            "imdb_search_mode": "unconfigured",
        }
    ]


def test_download_status_endpoint_returns_targeted_torrent(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_get_download_status(settings, info_hash, requester_id=None):
        assert info_hash == "abcdef1234567890"
        assert requester_id is None
        return {
            "name": "Ubuntu 24.04",
            "state": "downloading",
            "progress": 0.42,
            "size": 1234567,
            "seeds": 10,
            "hash": "abcdef1234567890",
        }

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "get_download_status_from_qbittorrent", fake_get_download_status)

    client = TestClient(app)
    response = client.get("/downloads/abcdef1234567890")

    assert response.status_code == 200
    assert response.json()["hash"] == "abcdef1234567890"


def test_downloads_endpoint_passes_user_filter_to_qbittorrent(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_list_downloads(settings, requester_id=None):
        assert requester_id == "telegram:123456789"
        return [
            {
                "name": "Ubuntu 24.04",
                "state": "downloading",
                "progress": 0.42,
                "size": 1234567,
                "seeds": 10,
                "hash": "abcdef1234567890",
            }
        ]

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "list_downloads_from_qbittorrent", fake_list_downloads)

    client = TestClient(app)
    response = client.get("/downloads?user_id=telegram:123456789")

    assert response.status_code == 200
    assert response.json()[0]["hash"] == "abcdef1234567890"


def test_download_status_endpoint_passes_user_filter_to_qbittorrent(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_get_download_status(settings, info_hash, requester_id=None):
        assert info_hash == "abcdef1234567890"
        assert requester_id == "telegram:123456789"
        return {
            "name": "Ubuntu 24.04",
            "state": "downloading",
            "progress": 0.42,
            "size": 1234567,
            "seeds": 10,
            "hash": "abcdef1234567890",
        }

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "get_download_status_from_qbittorrent", fake_get_download_status)

    client = TestClient(app)
    response = client.get("/downloads/abcdef1234567890?user_id=telegram:123456789")

    assert response.status_code == 200
    assert response.json()["hash"] == "abcdef1234567890"


def test_rendered_downloads_status_endpoint_returns_chat_message_and_watch_policy(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_list_downloads(settings, requester_id=None):
        assert requester_id == "telegram:123456789"
        return [
            TorrentStatus(
                name="Ubuntu 24.04",
                state="downloading",
                progress=0.5,
                size=1_000_000_000,
                seeds=10,
                hash="abcdef1234567890",
                download_speed=2_000_000,
                eta=600,
            )
        ]

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "list_downloads_from_qbittorrent", fake_list_downloads)

    client = TestClient(app)
    response = client.get("/downloads/status-message?user_id=telegram:123456789")

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == (
        "⬇️ Ubuntu 24.04\n"
        "🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50%\n"
        "💾 500 MB / 1 GB\n"
        "⚡ Speed: 2 MB/s\n"
        "⏱️ ETA: 10m"
    )
    assert payload["watch_policy"]["max_duration_seconds"] == 900
    assert payload["watch_policy"]["update_interval_seconds"] == 3
    assert payload["watch_policy"]["completion_notifications_are_separate"] is True
    assert payload["downloads"][0]["hash"] == "abcdef1234567890"


def test_rendered_download_status_endpoint_returns_single_status_message(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_get_download_status(settings, info_hash, requester_id=None):
        assert info_hash == "abcdef1234567890"
        assert requester_id == "telegram:123456789"
        return TorrentStatus(
            name="Ubuntu 24.04",
            state="downloading",
            progress=0.4,
            size=2_030_000_000,
            seeds=10,
            hash="abcdef1234567890",
            download_speed=12_400_000,
            eta=96,
        )

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "get_download_status_from_qbittorrent", fake_get_download_status)

    client = TestClient(app)
    response = client.get("/downloads/abcdef1234567890/status-message?user_id=telegram:123456789")

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == (
        "⬇️ Ubuntu 24.04\n"
        "🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 40%\n"
        "💾 812 MB / 2 GB\n"
        "⚡ Speed: 12.4 MB/s\n"
        "⏱️ ETA: 1m 36s"
    )
    assert payload["download"]["hash"] == "abcdef1234567890"


def test_pause_download_endpoint_requires_user_filter_and_returns_download(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_pause_download(settings, info_hash, requester_id):
        assert info_hash == "abcdef1234567890"
        assert requester_id == "telegram:123456789"
        return TorrentStatus(
            name="Ubuntu 24.04",
            state="stoppedDL",
            progress=0.4,
            size=2_030_000_000,
            seeds=10,
            hash="abcdef1234567890",
        )

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "pause_download_in_qbittorrent", fake_pause_download)

    client = TestClient(app)
    response = client.post("/downloads/abcdef1234567890/pause?user_id=telegram:123456789")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["action"] == "pause"
    assert payload["download"]["state"] == "stoppedDL"


def test_resume_download_endpoint_returns_404_when_requester_does_not_own_download(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_resume_download(settings, info_hash, requester_id):
        assert info_hash == "abcdef1234567890"
        assert requester_id == "telegram:99999"
        return None

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "resume_download_in_qbittorrent", fake_resume_download)

    client = TestClient(app)
    response = client.post("/downloads/abcdef1234567890/resume?user_id=telegram:99999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Download not found"


def test_delete_download_endpoint_keeps_files_and_returns_deleted_download(monkeypatch):
    import mpilot.api.downloads_list as downloads_api

    async def fake_delete_download(settings, info_hash, requester_id):
        assert info_hash == "abcdef1234567890"
        assert requester_id == "telegram:123456789"
        return TorrentStatus(
            name="Ubuntu 24.04",
            state="stoppedDL",
            progress=0.4,
            size=2_030_000_000,
            seeds=10,
            hash="abcdef1234567890",
        )

    monkeypatch.setattr(downloads_api, "get_settings", lambda: object())
    monkeypatch.setattr(downloads_api, "delete_download_from_qbittorrent", fake_delete_download)

    client = TestClient(app)
    response = client.post("/downloads/abcdef1234567890/delete?user_id=telegram:123456789")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["action"] == "delete"
    assert payload["download"]["hash"] == "abcdef1234567890"


def test_download_control_endpoint_requires_user_id():
    client = TestClient(app)
    response = client.post("/downloads/abcdef1234567890/pause")

    assert response.status_code == 422


def test_mcp_mount_exposes_acquisition_operations():
    openapi = app.openapi()
    handle_operation = openapi["paths"]["/handle"]["post"]
    snapshot_operation = openapi["paths"]["/queries/{query_id}"]["get"]
    old_operations = {
        operation.get("operationId")
        for path, methods in openapi["paths"].items()
        for operation in methods.values()
        if isinstance(operation, dict) and operation.get("operationId", "").startswith("qbitlarr_")
    }
    acquisition_operations = {
        operation.get("operationId")
        for path, methods in openapi["paths"].items()
        for operation in methods.values()
        if isinstance(operation, dict) and operation.get("operationId", "").startswith("acquisition_")
    }

    assert old_operations == set()
    assert handle_operation["operationId"] == "acquisition_handle"
    assert snapshot_operation["operationId"] == "acquisition_get_query_snapshot"
    assert acquisition_operations == {
        "acquisition_download",
        "acquisition_delete_download",
        "acquisition_get_download_status",
        "acquisition_get_query_snapshot",
        "acquisition_handle",
        "acquisition_health",
        "acquisition_list_downloads",
        "acquisition_list_indexers",
        "acquisition_pause_download",
        "acquisition_render_download_status",
        "acquisition_render_downloads_status",
        "acquisition_resume_download",
        "acquisition_search",
    }
    assert any(getattr(route, "path", "") == "/mcp" for route in app.routes)
