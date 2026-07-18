from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from mpilot.api.download import _snapshot_download_context
from mpilot.api.main import app
from mpilot.acquisition.client import AcquisitionApiClient
from mpilot.acquisition.domain.complementary_search import validate_complementary_results
from mpilot.acquisition.exceptions import UpstreamServiceError
from mpilot.acquisition.models import DownloadRequest, SearchResult
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore
from mpilot.acquisition.services.wikidata import resolve_imdb_metadata


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch):
    monkeypatch.setenv("MPILOT_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")
    monkeypatch.delenv("MPILOT_ACQUISITION_API_KEY", raising=False)
    monkeypatch.delenv("MPILOT_ACQUISITION_REQUESTER_API_KEYS", raising=False)

    async def deterministic_handle_metadata(imdb_id, settings):
        if imdb_id != "tt7587282":
            return None
        return {
            "imdb_id": imdb_id,
            "canonical_title": "Port Authority",
            "title_aliases": [],
            "year": 2019,
            "media_type": "movie",
            "metadata_source": "test",
        }

    monkeypatch.setattr("mpilot.api.handle.resolve_imdb_metadata", deterministic_handle_metadata)


def _settings(tmp_path, *, complementary_ids=None):
    return SimpleNamespace(
        query_snapshot_dir=str(tmp_path),
        prowlarr_complementary_indexer_ids=complementary_ids if complementary_ids is not None else [8, 9],
        request_timeout_seconds=1,
        manual_result_limit=4,
        choice_style="hermes-default",
        prefer_resolution="1080p",
        prefer_source="WEB-DL",
        prefer_codec="H.264",
        min_seeders=5,
        qbitlarr_save_path_movie="/downloads/movies",
        qbitlarr_save_path_movie_4k="/downloads/movies-4k",
        qbitlarr_save_path_tv="/downloads/tv",
        qbitlarr_extra_save_paths=[],
    )


def _result(
    title: str,
    link: str,
    *,
    info_hash: str | None = None,
    seeders: int = 10,
    size: int | None = None,
):
    return SearchResult(
        title=title,
        download_link=link,
        info_hash=info_hash,
        seeders=seeders,
        size=size,
        indexer="Complementary Source",
    )


def _create_snapshot(tmp_path, *, query_id="query-complementary", owner=None, status="imdb_empty"):
    QuerySnapshotStore(str(tmp_path)).create(
        query_id=query_id,
        request={
            "input": "tt7587282",
            "requester_id": owner,
            "query": "tt7587282",
            "imdb_id": "tt7587282",
            "media_type": "movie",
            "categories": [2040, 5040],
        },
        status=status,
        reason="imdb_no_results" if status == "imdb_empty" else "imdb_results_ready",
        results=[],
        metadata={"search_strategy": "imdb", "raw_result_count": 0},
    )


def test_validate_complementary_results_requires_contiguous_title_and_exact_nonconflicting_year():
    valid = _result("Port.Authority.2019.1080p.WEB-DL.H.264", "https://example.test/valid")
    unicode_valid = _result("PORT—AUTHORITY [2019] BluRay", "https://example.test/unicode")
    missing_year = _result("Port Authority 1080p", "https://example.test/missing")
    conflicting_year = _result("Port Authority 2019 Remaster 2024", "https://example.test/conflict")
    wrong_order = _result("Authority Port 2019", "https://example.test/order")
    duplicate_hash = _result(
        "Port Authority 2019 alternate", "https://example.test/duplicate", info_hash="ABC"
    )
    first_hash = _result("Port Authority 2019", "https://example.test/hash", info_hash="abc")
    duplicate_link_different_hash = _result(
        "Port Authority 2019 duplicate link",
        "https://example.test/hash",
        info_hash="different",
    )
    accent_equivalent = _result("Pórt Authority 2019", "https://example.test/accent")

    results = validate_complementary_results(
        [
            valid,
            unicode_valid,
            missing_year,
            conflicting_year,
            wrong_order,
            first_hash,
            duplicate_hash,
            duplicate_link_different_hash,
            accent_equivalent,
        ],
        canonical_title="Port Authority",
        year=2019,
    )

    assert [item.download_link for item in results] == [
        "https://example.test/valid",
        "https://example.test/unicode",
        "https://example.test/hash",
        "https://example.test/accent",
    ]


@pytest.mark.parametrize(
    ("imdb_id", "title", "year"),
    [
        ("tt7587282", "Port Authority", 2019),
        ("tt23861448", "Sarajevo Safari", 2022),
    ],
)
def test_resolve_imdb_metadata_builds_canonical_contract(monkeypatch, imdb_id, title, year):
    async def fake_run(query, settings, **kwargs):
        assert f'wdt:P345 "{imdb_id}"' in query
        assert kwargs == {"raise_on_error": True}
        return {
            "results": {
                "bindings": [
                    {
                        "item": {"value": "http://www.wikidata.org/entity/Q1"},
                        "itemLabel": {"value": title},
                        "alias": {"value": f"{title} Alias"},
                        "originalTitle": {"value": title},
                        "year": {"value": str(year)},
                        "type": {"value": "http://www.wikidata.org/entity/Q11424"},
                    }
                ]
            }
        }

    monkeypatch.setattr("mpilot.acquisition.services.wikidata._run_sparql", fake_run)
    metadata = asyncio.run(resolve_imdb_metadata(imdb_id, SimpleNamespace()))

    assert metadata == {
        "imdb_id": imdb_id,
        "canonical_title": title,
        "title_aliases": [f"{title} Alias"],
        "year": year,
        "media_type": "movie",
        "metadata_source": "wikidata",
        "wikidata_qid": "Q1",
    }


@pytest.mark.parametrize("mode", ["auto", "manual", "confirm"])
def test_zero_raw_imdb_results_returns_two_stage_complementary_action(monkeypatch, tmp_path, mode):
    async def empty_search(request, settings):
        assert request.query == "tt7587282"
        assert request.indexer_ids is None
        return []

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", empty_search)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.handle.create_query_id", lambda: "query-zero")

    payload = TestClient(app).post("/handle", json={"user_message": "tt7587282", "mode": mode}).json()

    assert payload["status"] == "success"
    assert payload["action"] == "complementary_search"
    assert payload["message_key"] == "imdb_empty_complementary_starting"
    assert payload["message_params"] == {}
    assert payload["message"].isascii()
    assert payload["query_id"] == "query-zero"
    assert payload["snapshot_status"] == "imdb_empty"
    assert payload["search_strategy"] == "imdb"
    snapshot = QuerySnapshotStore(str(tmp_path)).read("query-zero")
    assert snapshot.request["imdb_id"] == "tt7587282"
    assert snapshot.snapshots[0].metadata["raw_result_count"] == 0


def test_nonzero_low_quality_imdb_results_do_not_trigger_complementary(monkeypatch, tmp_path):
    async def low_quality_search(request, settings):
        return [_result("Port.Authority.2019.720p.WEBRip.H.265", "https://example.test/low", seeders=0)]

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", low_quality_search)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    response = TestClient(app).post("/handle", json={"user_message": "tt7587282", "mode": "auto"})
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "show_results"
    assert payload["snapshot_status"] == "imdb_ready"
    assert payload["action"] != "complementary_search"


def test_imdb_upstream_error_does_not_trigger_complementary(monkeypatch, tmp_path):
    async def failing_search(request, settings):
        raise UpstreamServiceError("Prowlarr is unreachable")

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", failing_search)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    response = TestClient(app).post("/handle", json={"user_message": "tt7587282"})

    assert response.status_code == 502
    assert response.json()["detail"] == "Prowlarr is unreachable"
    assert list(tmp_path.glob("*.json")) == []


def test_complementary_endpoint_uses_only_configured_indexers_and_never_downloads(monkeypatch, tmp_path):
    _create_snapshot(tmp_path)
    seen = {}

    async def metadata(imdb_id, settings):
        return {
            "imdb_id": imdb_id,
            "canonical_title": "Port Authority",
            "year": 2019,
            "media_type": "movie",
            "metadata_source": "wikidata",
        }

    async def search(request, settings):
        seen["request"] = request
        return [
            _result(
                "Port.Authority.2019.1080p.WEB-DL.H.264",
                "https://example.test/valid",
                size=3_100_000_000,
            ),
            _result("Port.Authority.2020.1080p.WEB-DL.H.264", "https://example.test/wrong"),
        ]

    monkeypatch.setattr("mpilot.api.complementary_search.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.complementary_search.resolve_imdb_metadata", metadata)
    monkeypatch.setattr("mpilot.api.complementary_search.search_prowlarr", search)

    response = TestClient(app).post(
        "/queries/query-complementary/complementary-search",
        json={"query": "caller controlled text must be ignored"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert seen["request"].query == "Port Authority 2019"
    assert seen["request"].indexer_ids == [8, 9]
    assert seen["request"].categories == [2040, 5040]
    assert seen["request"].result_resolution == "1080p"
    assert payload["action"] == "show_results"
    assert payload["query_used"] == "Port Authority 2019"
    assert payload["results_verified_by_imdb_id"] is False
    assert payload["message_key"] == "complementary_results_automatic"
    assert payload["message_params"] == {"query_used": "Port Authority 2019"}
    assert payload["message"].isascii()
    assert [result["title"] for result in payload["results"]] == [
        "Port.Authority.2019.1080p.WEB-DL.H.264"
    ]
    assert payload["results"][0]["indexer"] == "Complementary Source"
    assert payload["choices_table"] == (
        "1. Port.Authority.2019.1080p.WEB-DL.H.264\n"
        "   WEB-DL · H.264 · 🧲 10 · 💾 3.1GB · Complementary Source"
    )
    assert payload["choice_buttons"] == [{"index": 1, "text": "1", "value": "1"}]
    assert "Port.Authority.2019.1080p.WEB-DL.H.264" in payload["choice_rich_message"]["html"]
    assert "Complementary Source" in payload["choice_rich_message"]["html"]
    snapshot = QuerySnapshotStore(str(tmp_path)).read("query-complementary")
    assert snapshot.status == "complementary_ready"
    assert snapshot.snapshots[-1].metadata["trigger"] == "automatic_empty"
    assert snapshot.snapshots[-1].metadata["indexer_ids"] == [8, 9]


def test_user_requested_complementary_search_explains_that_results_may_repeat(monkeypatch, tmp_path):
    _create_snapshot(tmp_path, status="complementary_ready")

    async def metadata(imdb_id, settings):
        return {
            "imdb_id": imdb_id,
            "canonical_title": "Port Authority",
            "year": 2019,
            "media_type": "movie",
            "metadata_source": "wikidata",
        }

    async def search(request, settings):
        return [_result("Port.Authority.2019.1080p.WEB-DL.H.264", "https://example.test/valid")]

    monkeypatch.setattr("mpilot.api.complementary_search.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.complementary_search.resolve_imdb_metadata", metadata)
    monkeypatch.setattr("mpilot.api.complementary_search.search_prowlarr", search)

    payload = TestClient(app).post("/queries/query-complementary/complementary-search").json()

    assert payload["message_key"] == "complementary_results_user_requested"
    assert payload["message_params"] == {"query_used": "Port Authority 2019"}
    assert payload["message"].isascii()
    snapshot = QuerySnapshotStore(str(tmp_path)).read("query-complementary")
    assert snapshot.snapshots[-1].metadata["trigger"] == "user_requested"


def test_complementary_metadata_failure_never_runs_title_only_search(monkeypatch, tmp_path):
    _create_snapshot(tmp_path)

    async def no_metadata(imdb_id, settings):
        return None

    async def unexpected_search(request, settings):
        raise AssertionError("title-only complementary search must not run")

    monkeypatch.setattr("mpilot.api.complementary_search.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.complementary_search.resolve_imdb_metadata", no_metadata)
    monkeypatch.setattr("mpilot.api.complementary_search.search_prowlarr", unexpected_search)

    payload = TestClient(app).post("/queries/query-complementary/complementary-search").json()

    assert payload["status"] == "not_found"
    assert payload["snapshot_status"] == "complementary_metadata_unavailable"
    assert payload["message_key"] == "complementary_metadata_unavailable"
    assert payload["message_params"] == {}
    assert payload["query_used"] is None


def test_complementary_upstream_error_is_not_reported_as_empty(monkeypatch, tmp_path):
    _create_snapshot(tmp_path)

    async def metadata(imdb_id, settings):
        return {
            "canonical_title": "Port Authority",
            "year": 2019,
            "media_type": "movie",
            "metadata_source": "wikidata",
        }

    async def failing_search(request, settings):
        raise UpstreamServiceError("Prowlarr is unreachable")

    monkeypatch.setattr("mpilot.api.complementary_search.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.complementary_search.resolve_imdb_metadata", metadata)
    monkeypatch.setattr("mpilot.api.complementary_search.search_prowlarr", failing_search)

    response = TestClient(app).post("/queries/query-complementary/complementary-search")

    assert response.status_code == 502
    assert QuerySnapshotStore(str(tmp_path)).read("query-complementary").status == "complementary_error"


def test_complementary_metadata_upstream_error_is_not_reported_as_unavailable(monkeypatch, tmp_path):
    _create_snapshot(tmp_path)

    async def failing_metadata(imdb_id, settings):
        raise UpstreamServiceError("Wikidata is unreachable")

    async def unexpected_search(request, settings):
        raise AssertionError("Prowlarr must not run after metadata upstream failure")

    monkeypatch.setattr("mpilot.api.complementary_search.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.complementary_search.resolve_imdb_metadata", failing_metadata)
    monkeypatch.setattr("mpilot.api.complementary_search.search_prowlarr", unexpected_search)

    response = TestClient(app).post("/queries/query-complementary/complementary-search")

    assert response.status_code == 502
    snapshot = QuerySnapshotStore(str(tmp_path)).read("query-complementary")
    assert snapshot.status == "complementary_error"
    assert snapshot.snapshots[-1].reason == "complementary_metadata_error"


def test_complementary_endpoint_hides_cross_requester_snapshot(monkeypatch, tmp_path):
    _create_snapshot(tmp_path, owner="telegram:owner")
    monkeypatch.setenv(
        "MPILOT_ACQUISITION_REQUESTER_API_KEYS",
        json.dumps({"telegram:owner": "owner-key", "telegram:other": "other-key"}),
    )
    monkeypatch.setattr("mpilot.api.complementary_search.get_settings", lambda: _settings(tmp_path))

    response = TestClient(app).post(
        "/queries/query-complementary/complementary-search",
        headers={"X-API-Key": "other-key"},
    )

    assert response.status_code == 404


def test_original_query_id_resolves_complementary_download_context(tmp_path):
    _create_snapshot(tmp_path)
    selected = _result(
        "Port.Authority.2019.1080p.WEB-DL.H.264",
        "https://example.test/selected",
    ).model_copy(
        update={
            "verification_status": "title_year_validated",
            "verification_reason": "title_year",
        }
    )
    QuerySnapshotStore(str(tmp_path)).append(
        query_id="query-complementary",
        status="complementary_ready",
        reason="complementary_results_ready",
        results=[selected],
        metadata={"query_used": "Port Authority 2019"},
    )

    snapshot_input, snapshot_title, metadata = _snapshot_download_context(
        DownloadRequest(download_link=selected.download_link, query_id="query-complementary"),
        _settings(tmp_path),
    )

    assert snapshot_input == "tt7587282"
    assert snapshot_title == selected.title
    assert metadata["imdb_id"] == "tt7587282"


def test_acquisition_client_posts_complementary_endpoint_without_query_body():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"action": "show_results"})

    client = AcquisitionApiClient(
        "http://mpilot.test",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.complementary_search("query-123"))

    assert result["action"] == "show_results"
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/queries/query-123/complementary-search"
    assert requests[0].content == b""
