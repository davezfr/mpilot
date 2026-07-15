from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mpilot.api.handle import _auto_download_message, _select_best_verified_result
from mpilot.acquisition.domain.quality import (
    calculate_score,
    contains_premium_quality_request,
    extract_imdb_id,
    format_choice_label,
    format_quality,
    normalize_user_message,
    parse_quality,
)
from mpilot.api.main import app, get_categories
from mpilot.acquisition.models import SearchResult, TorrentStatus
from mpilot.acquisition.services.query_snapshots import QuerySnapshotStore

CHINESE_TEXT_RE = re.compile(r"[\u4e00-\u9fff]")


@pytest.fixture(autouse=True)
def allow_unauthenticated_loopback_for_handle_tests(monkeypatch):
    monkeypatch.setenv("MPILOT_ALLOW_UNAUTHENTICATED_LOOPBACK", "true")

    async def unresolved_imdb_metadata(imdb_id, settings):
        return None

    monkeypatch.setattr("mpilot.api.handle.resolve_imdb_metadata", unresolved_imdb_metadata)


def _result(title: str, *, seeders: int = 10, link_suffix: str | None = None) -> SearchResult:
    suffix = link_suffix or str(abs(hash(title)))
    return SearchResult(
        title=title,
        download_link=f"https://example.test/{suffix}.torrent",
        seeders=seeders,
        size=1_000_000,
        indexer="Indexer A",
    )


def _assert_english_message(payload: dict) -> None:
    assert payload["message"]
    assert not CHINESE_TEXT_RE.search(payload["message"])


def _settings(tmp_path):
    return SimpleNamespace(
        query_snapshot_dir=str(tmp_path),
        prowlarr_complementary_indexer_ids=[],
        qbitlarr_save_path_movie="/downloads/movies",
        qbitlarr_save_path_movie_4k="/downloads/movies-4k",
        qbitlarr_save_path_tv="/downloads/tv",
        qbitlarr_extra_save_paths=["/media/Kids"],
        default_mode="auto",
    )


def test_calculate_score_prefers_movie_1080p_webdl_h264_over_other_1080p_releases():
    webdl_h264 = _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP")
    webdl_h265 = _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.265-GRP", seeders=80)
    webrip_h264 = _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=120)
    bluray_remux = _result("The.Hitch-Hiker.1953.1080p.BluRay.REMUX.H.264-GRP", seeders=150)

    scores = {
        webdl_h264.title: calculate_score(webdl_h264, media_type="movie", prefer_premium=False),
        webdl_h265.title: calculate_score(webdl_h265, media_type="movie", prefer_premium=False),
        webrip_h264.title: calculate_score(webrip_h264, media_type="movie", prefer_premium=False),
        bluray_remux.title: calculate_score(bluray_remux, media_type="movie", prefer_premium=False),
    }

    assert scores[webdl_h264.title] > scores[webdl_h265.title]
    assert scores[webdl_h265.title] > scores[webrip_h264.title]
    assert scores[webrip_h264.title] > scores[bluray_remux.title]


def test_calculate_score_uses_seeders_first_for_single_digit_swarms():
    scarce_webdl = _result("Movie.2024.1080p.WEB-DL.H.264-GRP", seeders=6)
    scarce_bluray = _result("Movie.2024.1080p.BluRay.H.264-GRP", seeders=9)
    healthy_bluray = _result("Movie.2024.1080p.BluRay.H.264-HEALTHY", seeders=46)
    healthy_webdl = _result("Movie.2024.1080p.WEB-DL.H.264-HEALTHY", seeders=27)

    scarce_webdl_score = calculate_score(scarce_webdl, media_type="movie", prefer_premium=False)
    scarce_bluray_score = calculate_score(scarce_bluray, media_type="movie", prefer_premium=False)
    healthy_bluray_score = calculate_score(healthy_bluray, media_type="movie", prefer_premium=False)
    healthy_webdl_score = calculate_score(healthy_webdl, media_type="movie", prefer_premium=False)

    assert scarce_bluray_score > scarce_webdl_score
    assert healthy_bluray_score > scarce_webdl_score
    assert healthy_webdl_score > healthy_bluray_score


def test_calculate_score_filters_low_seeders_and_premium_request_prefers_2160p_remux():
    low_seeders = _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=4)
    normal_1080p = _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=200)
    premium_2160p = _result("The.Hitch-Hiker.1953.2160p.UHD.BluRay.REMUX.H.265-GRP", seeders=8)

    assert calculate_score(low_seeders, media_type="movie", prefer_premium=False) is None
    assert calculate_score(normal_1080p, media_type="movie", prefer_premium=True) is None
    assert calculate_score(premium_2160p, media_type="movie", prefer_premium=True) is not None
    assert contains_premium_quality_request("tt0045877 4K Remux") is True


def test_calculate_score_rejects_non_1080p_without_explicit_quality_request():
    high_seed_2160p = _result("The.Hitch-Hiker.1953.2160p.UHD.BluRay.REMUX.H.265-GRP", seeders=500)
    high_seed_720p = _result("The.Hitch-Hiker.1953.720p.WEB-DL.H.264-GRP", seeders=400)
    normal_1080p = _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=8)

    assert calculate_score(high_seed_2160p, media_type="movie", prefer_premium=False) is None
    assert calculate_score(high_seed_720p, media_type="movie", prefer_premium=False) is None
    assert calculate_score(normal_1080p, media_type="movie", prefer_premium=False) is not None
    assert calculate_score(high_seed_2160p, media_type="movie", prefer_premium=True) is not None


def test_calculate_score_prefers_tv_amzn_1080p_webdl_h264():
    amzn_h264 = _result("Example.Show.S03.1080p.AMZN.WEB-DL.H.264-GRP")
    amzn_h265 = _result("Example.Show.S03.1080p.AMZN.WEB-DL.H.265-GRP", seeders=80)
    other_h264 = _result("Example.Show.S03.1080p.WEB-DL.H.264-GRP", seeders=120)
    other_h265 = _result("Example.Show.S03.1080p.WEB-DL.H.265-GRP", seeders=150)

    assert calculate_score(amzn_h264, media_type="tv", prefer_premium=False) > calculate_score(
        amzn_h265,
        media_type="tv",
        prefer_premium=False,
    )
    assert calculate_score(amzn_h265, media_type="tv", prefer_premium=False) > calculate_score(
        other_h264,
        media_type="tv",
        prefer_premium=False,
    )
    assert calculate_score(other_h264, media_type="tv", prefer_premium=False) > calculate_score(
        other_h265,
        media_type="tv",
        prefer_premium=False,
    )


def test_parse_quality_returns_friendlier_quality_label():
    parsed = parse_quality("Example.Show.S03.1080p.AMZN.WEB-DL.H.264-GRP")

    assert format_quality(parsed) == "1080p WEB-DL H.264"


def test_format_quality_uses_english_unknown_label():
    parsed = parse_quality("Some.Release.Without.Quality.Markers-GRP")

    assert format_quality(parsed) == "Unknown quality"


def test_normalize_user_message_canonicalizes_imdb_links_from_messengers():
    raw_message = " <https://m.IMDb.com/title/TT0045877/?ref_=ext_shr_lnk&utm_source=whatsapp> "

    assert normalize_user_message(raw_message) == "https://www.imdb.com/title/tt0045877"
    assert extract_imdb_id(raw_message) == "tt0045877"


def test_get_categories_defaults_to_movie_and_tv_parent_categories():
    assert get_categories("The Hitch-Hiker") == [2000, 5000]


def test_get_categories_can_scope_canonical_movie_or_tv_type():
    assert get_categories("The Hitch-Hiker", media_type="movie") == [2000]
    assert get_categories("Example Show", media_type="tv") == [5000]


@pytest.mark.parametrize(
    "message",
    [
        "The Hitch-Hiker 4K",
        "The Hitch-Hiker 2160p",
        "The Hitch-Hiker UHD",
        "The Hitch-Hiker Remux",
    ],
)
def test_get_categories_uses_all_movie_and_tv_categories_for_premium_keywords(message):
    assert get_categories(message) == [2000, 5000]


@pytest.mark.parametrize(
    "message",
    [
        "The Hitch-Hiker H.265",
        "The Hitch-Hiker HEVC",
        "The Hitch-Hiker Atmos",
        "The Hitch-Hiker TrueHD",
        "The Hitch-Hiker DTS",
        "The Hitch-Hiker HDR",
    ],
)
def test_get_categories_keeps_parent_categories_for_non_uhd_quality_keywords(message):
    assert get_categories(message) == [2000, 5000]


def _candidate(title, *, imdb_id, year=None):
    return {"title": title, "year": year, "imdb_id": imdb_id, "wikidata_qid": None}


def test_handle_keyword_with_multiple_candidates_returns_choose_title(monkeypatch, tmp_path):
    async def fake_candidates(query, settings, *, limit=5):
        assert query == "The Hitch-Hiker"
        return [
            _candidate("The Hitch-Hiker", imdb_id="tt0045877", year=1953),
            _candidate("The Hitchhiker's Guide to the Galaxy", imdb_id="tt0371724", year=2005),
        ]

    async def unexpected_search_prowlarr(request, settings):
        raise AssertionError("Prowlarr must not be searched while the user is still picking a title")

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", unexpected_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.handle.create_query_id", lambda: "query-titles")

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "The Hitch-Hiker"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["action"] == "choose_title"
    _assert_english_message(payload)
    assert payload["query_id"] == "query-titles"
    assert payload["snapshot_status"] == "title_candidates"
    assert payload["results"] is None
    assert [c["index"] for c in payload["candidates"]] == [1, 2]
    assert [c["label"] for c in payload["candidates"]] == [
        "The Hitch-Hiker (1953)",
        "The Hitchhiker's Guide to the Galaxy (2005)",
    ]
    assert payload["candidates"][0]["imdb_id"] == "tt0045877"
    assert payload["choices_table"] == (
        "1. The Hitch-Hiker (1953)\n"
        "2. The Hitchhiker's Guide to the Galaxy (2005)"
    )
    assert payload["choice_display"] == (
        "I found a few possible matches. Reply with the number of the title you mean:\n\n"
        "```text\n"
        "1. The Hitch-Hiker (1953)\n"
        "2. The Hitchhiker's Guide to the Galaxy (2005)\n"
        "```"
    )
    assert payload["choice_buttons"] == [
        {"index": 1, "text": "1", "value": "1"},
        {"index": 2, "text": "2", "value": "2"},
    ]
    assert payload["ui_hints"]["closed_choice"] is True
    assert payload["choice_rich_message"]["format"] == "telegram-html"
    assert "<caption>Title choices</caption>" in payload["choice_rich_message"]["html"]
    assert "tt0045877" not in payload["choice_rich_message"]["html"]


def test_handle_keyword_with_single_candidate_passes_through_to_release_search(monkeypatch, tmp_path):
    queued: dict = {}

    async def fake_candidates(query, settings, *, limit=5):
        return [_candidate("The Shawshank Redemption", imdb_id="tt0111161", year=1994)]

    async def fake_search_prowlarr(request, settings):
        # The release search is keyed by the resolved IMDb ID, not the raw keyword.
        assert request.query == "tt0111161"
        return [_result("The.Shawshank.Redemption.1994.1080p.WEB-DL.H.264-GRP", seeders=50, link_suffix="shawshank")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "Shawshank Redemption"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "auto_download"
    assert payload["imdb_id"] == "tt0111161"
    assert queued["download_link"] == "https://example.test/shawshank.torrent"


def test_handle_keyword_with_no_candidates_asks_for_imdb(monkeypatch, tmp_path):
    async def fake_candidates(query, settings, *, limit=5):
        return []

    async def unexpected_search_prowlarr(request, settings):
        raise AssertionError("Prowlarr must not be searched when no title can be identified")

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", unexpected_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.handle.create_query_id", lambda: "query-needs-imdb")

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "asdkfjghqwer"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "not_found"
    assert payload["action"] == "needs_imdb"
    _assert_english_message(payload)
    assert payload["query_id"] == "query-needs-imdb"
    assert payload["snapshot_status"] == "keyword_unresolved"
    assert payload["candidates"] is None


def test_handle_keyword_choose_title_writes_title_candidates_snapshot(monkeypatch, tmp_path):
    async def fake_candidates(query, settings, *, limit=5):
        return [
            _candidate("Rare Movie", imdb_id="tt1000001", year=1971),
            _candidate("Rare Movie Returns", imdb_id="tt1000002", year=1985),
        ]

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.handle.create_query_id", lambda: "query-titles-snapshot")

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "Rare Movie"})

    assert response.status_code == 200
    assert response.json()["query_id"] == "query-titles-snapshot"

    snapshot = QuerySnapshotStore(str(tmp_path)).read("query-titles-snapshot")
    assert snapshot.status == "title_candidates"
    assert snapshot.snapshots[0].reason == "title_candidates_ready"


def test_get_query_snapshot_endpoint_returns_saved_snapshot(monkeypatch, tmp_path):
    store = QuerySnapshotStore(str(tmp_path))
    store.create(
        query_id="query-read",
        request={"input": "The Hitch-Hiker", "categories": [2040]},
        status="imdb_ready",
        reason="primary_results_ready",
        results=[_result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=50)],
    )
    monkeypatch.setattr("mpilot.api.query_snapshots.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.get("/queries/query-read")

    assert response.status_code == 200
    payload = response.json()
    assert payload["query_id"] == "query-read"
    assert payload["snapshots"][0]["results"][0]["title"] == "The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP"


def test_handle_keyword_passthrough_preserves_premium_quality_request(monkeypatch, tmp_path):
    async def fake_candidates(query, settings, *, limit=5):
        return [_candidate("The Hitch-Hiker", imdb_id="tt0045877", year=1953)]

    async def fake_search_prowlarr(request, settings):
        # Premium intent in the keyword still widens the categories on the resolved search.
        assert request.query == "tt0045877"
        assert request.categories == [2000, 5000]
        return [_result("The.Hitch-Hiker.1953.2160p.UHD.BluRay.REMUX.H.265-GRP", seeders=12, link_suffix="remux")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        return None

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "The Hitch-Hiker 4K Remux"})

    assert response.status_code == 200
    assert response.json()["action"] == "auto_download"


def test_handle_imdb_metadata_scopes_movie_search_before_prowlarr(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def movie_metadata(imdb_id, settings):
        assert imdb_id == "tt26446278"
        return {
            "imdb_id": imdb_id,
            "canonical_title": "The Count of Monte Cristo",
            "year": 2024,
            "media_type": "movie",
            "metadata_source": "wikidata",
        }

    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt26446278"
        assert request.categories == [2000]
        assert request.media_type == "movie"
        return [
            _result(
                "The.Count.Of.Monte.Cristo.2024.1080p.WEB-DL.H.264-GRP",
                seeders=20,
                link_suffix="monte-cristo",
            )
        ]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.resolve_imdb_metadata", movie_metadata)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    response = TestClient(app).post("/handle", json={"user_message": "tt26446278"})

    assert response.status_code == 200
    assert response.json()["media_type"] == "movie"
    assert queued == {
        "download_link": "https://example.test/monte-cristo.torrent",
        "save_path": "/downloads/movies",
    }


def test_handle_imdb_id_auto_downloads_best_movie_to_movie_path(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def fake_search_prowlarr(request, settings):
        assert request.identifier is None
        assert request.query == "tt0045877"
        assert request.categories == [2000, 5000]
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=120, link_suffix="webrip"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.265-GRP", seeders=80, link_suffix="h265"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=9, link_suffix="h264"),
        ]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path
        queued["requester_id"] = requester_id

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877", "user_id": "telegram:123456789"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["action"] == "auto_download"
    assert payload["imdb_id"] == "tt0045877"
    assert payload["media_type"] == "movie"
    assert payload["title"] == "The Hitch-Hiker (1953)"
    assert payload["quality"] == "1080p WEB-DL H.265"
    assert payload["message"] == (
        "The Hitch-Hiker (1953) is now downloading with 80 seeders. "
        "You can ask for a status update any time."
    )
    _assert_english_message(payload)
    assert queued == {
        "download_link": "https://example.test/h265.torrent",
        "save_path": "/downloads/movies",
        "requester_id": "telegram:123456789",
    }


def test_handle_imdb_id_auto_downloads_4k_movie_to_4k_movie_path(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt0045877"
        assert request.categories == [2000, 5000]
        return [_result("The.Hitch-Hiker.1953.2160p.UHD.BluRay.REMUX.H.265-GRP", seeders=50, link_suffix="2160")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877 4K"})

    assert response.status_code == 200
    assert response.json()["action"] == "auto_download"
    assert queued == {
        "download_link": "https://example.test/2160.torrent",
        "save_path": "/downloads/movies-4k",
    }


def test_handle_imdb_id_auto_downloads_tv_to_tv_path(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def tv_metadata(imdb_id, settings):
        return {
            "imdb_id": imdb_id,
            "canonical_title": "Example Show",
            "year": 2025,
            "media_type": "tv",
            "metadata_source": "wikidata",
        }

    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt0017925"
        assert request.categories == [5000]
        assert request.media_type == "tv"
        return [_result("Example.Show.S03.1080p.AMZN.WEB-DL.H.264-GRP", seeders=50, link_suffix="tv")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.resolve_imdb_metadata", tv_metadata)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0017925"})

    assert response.status_code == 200
    assert response.json()["action"] == "auto_download"
    assert queued == {
        "download_link": "https://example.test/tv.torrent",
        "save_path": "/downloads/tv/Example Show",
    }


def test_handle_imdb_id_save_path_override_takes_precedence(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt0045877"
        return [_result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=50, link_suffix="h264")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post(
        "/handle",
        json={"user_message": "tt0045877", "save_path": "/media/Kids"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "auto_download"
    assert queued == {
        "download_link": "https://example.test/h264.torrent",
        "save_path": "/media/Kids",
    }


def test_handle_imdb_id_auto_download_message_uses_selected_seeders_not_transient_qbittorrent_status(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt0045877"
        return [_result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=50, link_suffix="h264")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        return TorrentStatus(
            name="The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP",
            state="downloading",
            progress=0.25,
            size=8_000_000_000,
            seeds=12,
            hash="abcdef",
            download_speed=2_000_000,
            eta=600,
        )

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    message = payload["message"]
    assert message == (
        "The Hitch-Hiker (1953) is now downloading with 50 seeders. "
        "You can ask for a status update any time."
    )
    assert payload["download_status"] == {
        "name": "The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP",
        "state": "downloading",
        "progress": 0.25,
        "size": 8_000_000_000,
        "seeds": 12,
        "hash": "abcdef",
        "download_speed": 2_000_000,
        "eta": 600,
        "content_path": None,
    }


def test_auto_download_message_keeps_existing_download_copy_short():
    message = _auto_download_message(
        "Within Our Gates (1920)",
        1,
        already_downloading=True,
    )

    assert message == "Within Our Gates (1920) is already in the system with 1 seeder. You can ask for a status update any time."


def test_auto_download_message_omits_seeder_count_when_unknown():
    message = _auto_download_message("Within Our Gates (1920)", None)

    assert message == "Within Our Gates (1920) is now downloading. You can ask for a status update any time."


def test_handle_imdb_id_never_refines_by_title_before_auto_download(monkeypatch, tmp_path):
    queued: dict[str, str] = {}
    calls: list[str | None] = []

    async def fake_search_prowlarr(request, settings):
        calls.append(request.query)
        if request.query == "tt0045877":
            return [_result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=120, link_suffix="webrip")]
        raise AssertionError(f"unexpected query: {request.query}")

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    assert response.json()["quality"] == "1080p WEBRip H.264"
    assert queued["download_link"] == "https://example.test/webrip.torrent"
    assert calls == ["tt0045877"]


def test_handle_imdb_id_uses_torrent_metadata_title_for_auto_selection(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt0045877"
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-BAD", seeders=500, link_suffix="bad"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GOOD", seeders=50, link_suffix="good"),
        ]

    async def fake_torrent_metadata_title(result, settings):
        if result.download_link.endswith("/bad.torrent"):
            return "The.Hitch-Hiker.1953.1080p.WEBRip.H.264-BAD"
        if result.download_link.endswith("/good.torrent"):
            return "The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GOOD"
        return None

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle._get_torrent_metadata_title", fake_torrent_metadata_title, raising=False)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    assert response.json()["quality"] == "1080p WEB-DL H.264"
    assert queued["download_link"] == "https://example.test/good.torrent"


def test_verified_auto_selection_checks_initial_metadata_batch_concurrently(monkeypatch):
    active_checks = 0
    max_active_checks = 0

    async def fake_torrent_metadata_title(result, settings):
        nonlocal active_checks, max_active_checks
        active_checks += 1
        max_active_checks = max(max_active_checks, active_checks)
        await asyncio.sleep(0)
        active_checks -= 1
        if result.download_link.endswith("/bad.torrent"):
            return "The.Hitch-Hiker.1953.1080p.WEBRip.H.264-BAD"
        return result.title

    monkeypatch.setattr("mpilot.api.handle._get_torrent_metadata_title", fake_torrent_metadata_title, raising=False)

    selected = asyncio.run(
        _select_best_verified_result(
            [
                _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-BAD", seeders=500, link_suffix="bad"),
                _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GOOD", seeders=50, link_suffix="good"),
            ],
            SimpleNamespace(),
            media_type="movie",
            prefer_premium=False,
        )
    )

    assert max_active_checks == 2
    assert selected is not None
    assert selected.download_link == "https://example.test/good.torrent"


def test_handle_imdb_id_returns_existing_matching_download_without_readding(monkeypatch, tmp_path):
    search_queries: list[str] = []
    tagged: list[tuple[str, str | None]] = []

    async def fake_search_prowlarr(request, settings):
        search_queries.append(request.query)
        if request.query == "tt0045877":
            return [_result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-BAD", seeders=100)]
        raise AssertionError("existing qBittorrent match should avoid title refinement")

    async def fake_list_downloads(settings):
        return [
            TorrentStatus(
                name="The.Hitch-Hiker.1953.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP",
                state="stalledDL",
                progress=0.0,
                size=8_000_000_000,
                seeds=0,
                hash="abcdef",
            )
        ]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        raise AssertionError("existing qBittorrent match should not be added again")

    async def fake_tag_download(settings, info_hash, requester_id):
        tagged.append((info_hash, requester_id))
        return "requester.telegram-123456789"

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.list_downloads_from_qbittorrent", fake_list_downloads, raising=False)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.tag_download_for_requester", fake_tag_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877", "user_id": "telegram:123456789"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "auto_download"
    assert payload["imdb_id"] == "tt0045877"
    assert payload["media_type"] == "movie"
    assert payload["title"] == "The Hitch-Hiker (1953)"
    assert payload["quality"] == "1080p WEB-DL H.264"
    assert payload["snapshot_status"] == "already_in_qbittorrent"
    assert search_queries == ["tt0045877"]
    assert tagged == [("abcdef", "telegram:123456789")]


def test_handle_imdb_shared_url_searches_embedded_id_as_keyword(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def fake_search_prowlarr(request, settings):
        assert request.identifier is None
        assert request.query == "tt0045877"
        assert request.categories == [2000, 5000]
        return [_result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=50, link_suffix="h264")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post(
        "/handle",
        json={"user_message": "https://www.imdb.com/title/tt0045877/?ref_=ext_shr_lnk&utm_source=telegram"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "auto_download"
    assert queued["download_link"] == "https://example.test/h264.torrent"


def test_handle_douban_movie_url_resolves_to_imdb_and_auto_downloads(monkeypatch, tmp_path):
    queued: dict[str, str] = {}

    async def fake_resolve_external_movie_id(user_message, settings):
        assert user_message == "https://movie.douban.com/subject/1292052/"
        return {
            "source": "douban",
            "source_id": "1292052",
            "imdb_id": "tt0111161",
            "wikidata_qid": "Q172241",
        }

    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt0111161"
        return [_result("The.Shawshank.Redemption.1994.1080p.WEB-DL.H.264-GRP", seeders=50, link_suffix="shawshank")]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link
        queued["save_path"] = save_path

    monkeypatch.setattr("mpilot.api.handle.resolve_external_movie_id", fake_resolve_external_movie_id, raising=False)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "https://movie.douban.com/subject/1292052/"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "auto_download"
    assert payload["imdb_id"] == "tt0111161"
    assert payload["media_type"] == "movie"
    assert payload["title"] == "The Shawshank Redemption (1994)"
    assert queued == {
        "download_link": "https://example.test/shawshank.torrent",
        "save_path": "/downloads/movies",
    }


def test_handle_allocine_movie_url_unresolved_asks_for_imdb(monkeypatch, tmp_path):
    async def fake_resolve_external_movie_id(user_message, settings):
        assert user_message == "https://www.allocine.fr/film/fichefilm_gen_cfilm=25801.html"
        return {
            "source": "allocine",
            "source_id": "25801",
            "imdb_id": None,
            "wikidata_qid": None,
        }

    async def unexpected_search_prowlarr(request, settings):
        raise AssertionError("search_prowlarr should not run when external movie resolution fails")

    monkeypatch.setattr("mpilot.api.handle.resolve_external_movie_id", fake_resolve_external_movie_id, raising=False)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", unexpected_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("mpilot.api.handle.create_query_id", lambda: "query-allocine-unresolved")

    client = TestClient(app)
    response = client.post(
        "/handle",
        json={"user_message": "https://www.allocine.fr/film/fichefilm_gen_cfilm=25801.html"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "not_found"
    assert payload["action"] == "needs_imdb"
    assert payload["message"] == (
        "I couldn't match that link to a movie reliably. "
        "For faster and more precise results, please send the IMDb link or IMDb ID instead."
    )
    assert payload["query_id"] == "query-allocine-unresolved"
    assert payload["snapshot_status"] == "external_id_unresolved"
    assert payload["results"] == []


def test_handle_keyword_choose_title_label_falls_back_to_title_without_year(monkeypatch, tmp_path):
    async def fake_candidates(query, settings, *, limit=5):
        return [
            _candidate("Some Untitled Doc", imdb_id="tt9000001"),
            _candidate("Another Match", imdb_id="tt9000002", year=2011),
        ]

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "untitled doc"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "choose_title"
    assert payload["candidates"][0]["label"] == "Some Untitled Doc"
    assert payload["candidates"][1]["label"] == "Another Match (2011)"


def test_handle_imdb_id_returns_manual_list_when_no_result_meets_seed_threshold(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        assert request.identifier is None
        assert request.categories == [2000, 5000]
        if request.query == "tt0017925":
            return [
                _result("Example.Show.S03.1080p.AMZN.WEB-DL.H.264-GRP", seeders=4, link_suffix="low"),
                _result("Example.Show.S03.720p.WEB-DL.H.264-GRP", seeders=3, link_suffix="lower"),
            ]
        if request.query == "Example Show S03":
            return []
        raise AssertionError(f"unexpected query: {request.query}")

    async def fail_if_downloaded(download_link, settings, *, save_path=None):
        raise AssertionError("download should not be queued")

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fail_if_downloaded)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0017925"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "not_found"
    assert payload["action"] == "show_results"
    assert payload["message"] == "No suitable auto-download found. Here are the top results, please reply with the number:"
    _assert_english_message(payload)
    assert len(payload["results"]) == 2
    assert payload["results"][0]["title"] == "Example.Show.S03.1080p.AMZN.WEB-DL.H.264-GRP"
    assert payload["results"][1]["title"] == "Example.Show.S03.720p.WEB-DL.H.264-GRP"


def _settings_with_prefs(tmp_path, **overrides):
    base = _settings(tmp_path)
    defaults = {
        "prefer_resolution": "1080p",
        "prefer_source": "WEB-DL",
        "prefer_codec": "H.264",
        "min_seeders": 5,
        "default_mode": "auto",
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(base, key, value)
    return base


def test_handle_mode_manual_skips_auto_download_and_returns_ranked_results(monkeypatch, tmp_path):
    queued: dict = {}

    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.720p.BluRay.H.264-GRP", seeders=500, link_suffix="720p"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.265-GRP", seeders=40, link_suffix="h265"),
        ]

    async def fail_if_downloaded(*args, **kwargs):
        queued["called"] = True

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fail_if_downloaded)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877", "mode": "manual"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    assert "called" not in queued
    assert payload["results"][0]["title"] == "The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP"
    assert any(result["title"].endswith("720p.BluRay.H.264-GRP") for result in payload["results"])


def test_handle_mode_manual_ranks_healthy_blurays_before_single_digit_webdls(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        results = [
            _result("Movie.2024.1080p.WEB-DL.H.264-SIX", seeders=6, link_suffix="webdl-6"),
            _result("Movie.2024.1080p.WEB-DL.H.264-ONE", seeders=1, link_suffix="webdl-1"),
            _result("Movie.2024.1080p.BluRay.x264-FORTYSIX", seeders=46, link_suffix="bluray-46"),
            _result("Movie.2024.1080p.BluRay.x264-TWENTYSEVEN", seeders=27, link_suffix="bluray-27"),
            _result("Movie.2024.1080p.BluRay.x264-TWENTYTWO", seeders=22, link_suffix="bluray-22"),
        ]
        return [result.model_copy(update={"size": (index + 1) * 1_000_000}) for index, result in enumerate(results)]

    settings = _settings_with_prefs(tmp_path, default_mode="manual", manual_result_limit=5)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: settings)

    response = TestClient(app).post("/handle", json={"user_message": "tt26446278"})

    assert response.status_code == 200
    assert [result["seeders"] for result in response.json()["results"]] == [46, 27, 22, 6, 1]


def test_handle_auto_selects_healthy_bluray_over_single_digit_webdl(monkeypatch, tmp_path):
    queued = {}

    async def fake_search_prowlarr(request, settings):
        return [
            _result("Movie.2024.1080p.WEB-DL.H.264-GRP", seeders=6, link_suffix="webdl-6"),
            _result("Movie.2024.1080p.BluRay.x264-GRP", seeders=46, link_suffix="bluray-46"),
        ]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        queued["download_link"] = download_link

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path))

    response = TestClient(app).post("/handle", json={"user_message": "tt26446278"})

    assert response.status_code == 200
    assert response.json()["action"] == "auto_download"
    assert queued["download_link"] == "https://example.test/bluray-46.torrent"


def test_handle_mode_manual_prefers_direct_peer_links_and_skips_unavailable_http_results(monkeypatch, tmp_path):
    unavailable_link = "http://prowlarr.test/7/download?link=bad"
    available_link = "http://prowlarr.test/6/download?link=good"
    magnet_link = "magnet:?xt=urn:btih:abcdef&dn=The.Hitch-Hiker"

    async def fake_search_prowlarr(request, settings):
        return [
            SearchResult(
                title="The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-BAD",
                download_link=unavailable_link,
                seeders=500,
                size=3_000_000_000,
                indexer="Unstable",
            ),
            SearchResult(
                title="The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-MAGNET",
                download_link=magnet_link,
                seeders=40,
                size=2_000_000_000,
                indexer="Direct",
            ),
            SearchResult(
                title="The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GOOD",
                download_link=available_link,
                seeders=30,
                size=1_000_000_000,
                indexer="Stable",
            ),
        ]

    async def fake_source_available(result, settings):
        return result.download_link != unavailable_link

    settings = _settings_with_prefs(tmp_path, default_mode="manual", manual_result_limit=2)
    settings.prowlarr_url = "http://prowlarr.test"
    settings.prowlarr_api_key = "secret"
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle._is_download_source_available", fake_source_available)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: settings)

    response = TestClient(app).post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    links = [result["download_link"] for result in response.json()["results"]]
    assert links == [magnet_link, available_link]


def test_handle_mode_manual_surfaces_only_lower_quality_imdb_results(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            _result("Port.Authority.2019.720p.BluRay.H.264-GRP", seeders=2, link_suffix="720p"),
            _result("Port.Authority.2019.DVDRip.x264-GRP", seeders=3, link_suffix="dvdrip"),
        ]

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path))

    response = TestClient(app).post("/handle", json={"user_message": "tt7587282", "mode": "manual"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert [result["title"] for result in payload["results"]] == [
        "Port.Authority.2019.DVDRip.x264-GRP",
        "Port.Authority.2019.720p.BluRay.H.264-GRP",
    ]


def test_handle_mode_confirm_returns_top_pick_with_alternatives_no_download(monkeypatch, tmp_path):
    queued: dict = {}

    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.265-GRP", seeders=40, link_suffix="h265"),
            _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=20, link_suffix="webrip"),
        ]

    async def fail_if_downloaded(*args, **kwargs):
        queued["called"] = True

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fail_if_downloaded)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877", "mode": "confirm"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "confirm"
    assert "called" not in queued
    assert "acquisition_download" in payload["message"]
    assert "qbitlarr_download" not in payload["message"]
    assert payload["results"][0]["title"] == "The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP"
    alternatives = payload["alternatives"]
    assert len(alternatives) >= 1
    assert all(alt["title"] != "The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP" for alt in alternatives)


def test_handle_auto_download_includes_alternatives_inline(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.265-GRP", seeders=40, link_suffix="h265"),
            _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=20, link_suffix="webrip"),
        ]

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        return None

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "auto_download"
    alternatives = payload["alternatives"]
    assert len(alternatives) >= 1
    assert all(alt["download_link"] != "https://example.test/h264.torrent" for alt in alternatives)


def test_handle_default_mode_env_var_can_force_manual(monkeypatch, tmp_path):
    queued: dict = {}

    async def fake_search_prowlarr(request, settings):
        return [_result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264")]

    async def fail_if_downloaded(*args, **kwargs):
        queued["called"] = True

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fail_if_downloaded)
    monkeypatch.setattr(
        "mpilot.api.handle.get_settings",
        lambda: _settings_with_prefs(tmp_path, default_mode="manual"),
    )

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    assert "called" not in queued


def test_preference_env_vars_rank_default_match_before_other_resolutions(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.720p.WEB-DL.H.265-GRP", seeders=80, link_suffix="720"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="1080"),
        ]

    async def fake_candidates(query, settings, *, limit=5):
        return [_candidate("The Hitch-Hiker", imdb_id="tt0045877", year=1953)]

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr(
        "mpilot.api.handle.get_settings",
        lambda: _settings_with_prefs(tmp_path, prefer_resolution="720p", prefer_codec="H.265", default_mode="manual"),
    )

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "The Hitch-Hiker"})

    assert response.status_code == 200
    payload = response.json()
    titles = [r["title"] for r in payload["results"]]
    assert titles == [
        "The.Hitch-Hiker.1953.720p.WEB-DL.H.265-GRP",
        "The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP",
    ]


def test_format_choice_label_keeps_differentiators_and_drops_default_resolution():
    assert format_choice_label(parse_quality("In.the.Grey.2026.1080p.WEBRip.10Bit.DDP.5.1.x265-NeoNoir")) == "WEBRip · H.265"
    assert format_choice_label(parse_quality("In The Grey 2026 1080p WEB-DL HEVC x265 5.1 BONE")) == "WEB-DL · H.265"
    assert format_choice_label(parse_quality("Movie.2026.2160p.UHD.BluRay.REMUX.HEVC-GRP")) == "2160p · REMUX · H.265"
    assert format_choice_label(parse_quality("Some.Release.Without.Quality.Markers-GRP")) == "Unknown quality"


def test_handle_imdb_manual_results_use_compact_labels_and_dedupe_same_release(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            SearchResult(
                title="In.the.Grey.2026.1080p.WEBRip.x265-NeoNoir",
                download_link="https://example.test/a.torrent",
                seeders=3181,
                size=2_100_000_000,
                indexer="Indexer A",
            ),
            SearchResult(
                title="In.the.Grey.2026.1080p.WEBRip.10Bit.DDP.5.1.x265-NeoNoir",
                download_link="https://example.test/b.torrent",
                seeders=1948,
                size=2_100_000_000,
                indexer="Indexer B",
            ),
            SearchResult(
                title="In The Grey 2026 1080p WEB-DL HEVC x265 5.1 BONE",
                download_link="https://example.test/c.torrent",
                seeders=1410,
                size=1_600_000_000,
                indexer="Indexer C",
            ),
        ]

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877", "mode": "manual"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    labels = [r["label"] for r in payload["results"]]
    assert sorted(labels) == ["WEB-DL · H.265", "WEBRip · H.265"]
    kept_webrip = next(r for r in payload["results"] if r["label"] == "WEBRip · H.265")
    assert kept_webrip["seeders"] == 3181
    assert kept_webrip["title"] == "In.the.Grey.2026.1080p.WEBRip.x265-NeoNoir"


def test_handle_second_stage_imdb_from_picker_returns_release_choices(monkeypatch, tmp_path):
    # After choose_title, the agent re-calls /handle with the picked candidate's
    # IMDb ID, which lands on the same unified release-choice flow.
    async def fake_search_prowlarr(request, settings):
        assert request.query == "tt0045877"
        return [_result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264")]

    async def fail_download(*args, **kwargs):
        raise AssertionError("manual mode must not auto-download")

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fail_download)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path, default_mode="manual"))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    assert payload["choices_table"] is not None


def test_settings_default_mode_is_manual():
    from mpilot.acquisition.config import Settings

    assert Settings.__dataclass_fields__["default_mode"].default == "manual"


def test_resolve_mode_defaults_to_manual_for_minimal_settings():
    from mpilot.api.handle import _resolve_mode

    assert _resolve_mode(None, SimpleNamespace()) == "manual"


def test_handle_imdb_without_mode_returns_choices_not_auto_download(monkeypatch, tmp_path):
    queued: dict = {}

    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=40, link_suffix="webrip"),
        ]

    async def fail_if_downloaded(*args, **kwargs):
        queued["called"] = True

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.add_download_to_qbittorrent", fail_if_downloaded)
    monkeypatch.setattr(
        "mpilot.api.handle.get_settings",
        lambda: _settings_with_prefs(tmp_path, default_mode="manual"),
    )

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    assert "called" not in queued
    labels = [r["label"] for r in payload["results"]]
    assert "WEB-DL · H.264" in labels
    assert "WEBRip · H.264" in labels


def test_render_choice_table_aligns_columns_and_marks_recommended():
    from mpilot.acquisition.domain.choice_table import render_choice_table
    from mpilot.acquisition.models import ManualSearchResult

    results = [
        ManualSearchResult(index=1, title="Toy.Story.4.2019.1080p.BluRay.DTS-GRP", quality="1080p BluRay", seeders=14210, size=1_600_000_000, download_link="https://example.test/1.torrent"),
        ManualSearchResult(index=2, title="Toy.Story.4.2019.1080p.BluRay.x264-GRP", quality="1080p BluRay H.264", seeders=210, size=3_400_000_000, download_link="https://example.test/2.torrent"),
        ManualSearchResult(index=3, title="Toy.Story.4.2019.1080p.BluRay.x265-GRP", quality="1080p BluRay H.265", seeders=91, size=12_500_000_000, download_link="https://example.test/3.torrent"),
    ]

    table = render_choice_table(results)
    lines = table.splitlines()

    assert lines[0].startswith("1.")
    assert lines[1].startswith("2.")
    assert len(lines) == 3
    # Same emojis at the same column on every row keeps alignment.
    assert len({line.index("🧲") for line in lines}) == 1
    assert len({line.index("💾") for line in lines}) == 1
    assert "14210" in lines[0]
    assert "1.6GB" in lines[0]
    assert "12.5GB" in lines[2]


def test_render_unverified_choice_table_keeps_full_title_and_indexer():
    from mpilot.acquisition.domain.choice_table import render_unverified_choice_table
    from mpilot.acquisition.models import ManualSearchResult

    results = [
        ManualSearchResult(
            index=1,
            title="Sarajevo.Safari.2022.1080p.HDTV.x264",
            quality="1080p HDTV H.264",
            seeders=1,
            size=3_100_000_000,
            download_link="https://example.test/1.torrent",
            indexer="RuTracker",
        ),
        ManualSearchResult(
            index=2,
            title="Sarajevo.Safari.2022.SLO.1080p.HDTV.x264",
            quality="1080p HDTV H.264",
            seeders=1,
            size=2_100_000_000,
            download_link="https://example.test/2.torrent",
            indexer="52BT",
        ),
    ]

    assert render_unverified_choice_table(results) == (
        "1. Sarajevo.Safari.2022.1080p.HDTV.x264\n"
        "   HDTV · H.264 · 🧲 1 · 💾 3.1GB · RuTracker\n\n"
        "2. Sarajevo.Safari.2022.SLO.1080p.HDTV.x264\n"
        "   HDTV · H.264 · 🧲 1 · 💾 2.1GB · 52BT"
    )


def test_handle_imdb_show_results_includes_choices_table(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.265-GRP", seeders=40, link_suffix="webrip"),
        ]

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path, default_mode="manual"))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    table = payload["choices_table"]
    assert table is not None
    first_line = table.splitlines()[0]
    assert first_line.startswith("1.")
    assert "★" not in table  # recommendation is conveyed by the starred button, not the table
    assert "🧲" in table and "💾" in table


def test_handle_imdb_show_results_defaults_to_four_choices_for_stock_hermes(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.265-GRP", seeders=70, link_suffix="h265"),
            _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=60, link_suffix="webrip-h264"),
            _result("The.Hitch-Hiker.1953.1080p.BluRay.H.264-GRP", seeders=50, link_suffix="bluray-h264"),
            _result("The.Hitch-Hiker.1953.1080p.BluRay.H.265-GRP", seeders=40, link_suffix="bluray-h265"),
        ]

    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings_with_prefs(tmp_path, default_mode="manual"))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    assert len(payload["results"]) == 4
    assert len(payload["choice_buttons"]) == 4
    assert payload["choice_buttons"] == [
        {"index": 1, "text": "1", "value": "1"},
        {"index": 2, "text": "2", "value": "2"},
        {"index": 3, "text": "3", "value": "3"},
        {"index": 4, "text": "4", "value": "4"},
    ]
    assert payload["ui_hints"] == {
        "choice_style": "hermes-default",
        "recommended_button_layout": "vertical",
        "closed_choice": True,
    }
    assert payload["choice_display"].startswith("Here are the top results")
    assert "```text" in payload["choice_display"]
    assert len(payload["choices_table"].splitlines()) == 4


def test_handle_imdb_show_results_can_return_five_choices_for_custom_telegram_rich(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.265-GRP", seeders=70, link_suffix="h265"),
            _result("The.Hitch-Hiker.1953.1080p.WEBRip.H.264-GRP", seeders=60, link_suffix="webrip-h264"),
            _result("The.Hitch-Hiker.1953.1080p.BluRay.H.264-GRP", seeders=50, link_suffix="bluray-h264"),
            _result("The.Hitch-Hiker.1953.1080p.BluRay.H.265-GRP", seeders=40, link_suffix="bluray-h265"),
        ]

    settings = _settings_with_prefs(
        tmp_path,
        default_mode="manual",
        manual_result_limit=5,
        choice_style="telegram-rich",
    )
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: settings)

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "show_results"
    assert len(payload["results"]) == 5
    assert len(payload["choice_buttons"]) == 5
    assert payload["choice_buttons"][-1] == {"index": 5, "text": "5", "value": "5"}
    assert payload["ui_hints"] == {
        "choice_style": "telegram-rich",
        "recommended_button_layout": "inline-row",
        "closed_choice": True,
    }
    assert payload["choices_table"] is None
    assert payload["choice_display"].startswith("Here are the top results")
    assert "```text" not in payload["choice_display"]
    assert "1. 1." not in payload["choice_display"]
    assert len(
        [
            line
            for line in payload["choice_display"].splitlines()
            if line.startswith(("1.", "2.", "3.", "4.", "5."))
        ]
    ) == 5


def test_handle_imdb_show_results_includes_telegram_rich_table_without_links(monkeypatch, tmp_path):
    async def fake_search_prowlarr(request, settings):
        return [
            _result("The.Hitch-Hiker.1953.1080p.WEB-DL.H.264-GRP", seeders=80, link_suffix="h264"),
            _result("The.Hitch-Hiker.1953.2160p.UHD.BluRay.REMUX.H.265-GRP", seeders=50, link_suffix="remux"),
        ]

    settings = _settings_with_prefs(
        tmp_path,
        default_mode="manual",
        manual_result_limit=5,
        choice_style="telegram-rich",
    )
    monkeypatch.setattr("mpilot.api.handle.search_prowlarr", fake_search_prowlarr)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: settings)

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "tt0045877 4K"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["choices_table"] is None
    assert "```text" not in payload["choice_display"]
    rich_message = payload["choice_rich_message"]
    assert rich_message["format"] == "telegram-html"
    assert rich_message["skip_entity_detection"] is True
    html = rich_message["html"]
    assert html.startswith("<p><b>Here are the top results")
    assert "<table bordered striped>" in html
    assert "<th>#</th>" in html
    assert "<th>Resolution</th>" in html
    assert '<td align="right"><b>1</b></td>' in html
    assert '<td align="right">50</td>' in html
    assert "2160p" in html
    assert "REMUX" in html
    assert "https://example.test" not in html
    assert "<pre>" not in html


def test_handle_keyword_choose_title_telegram_rich_omits_raw_table_and_markdown(monkeypatch, tmp_path):
    async def fake_candidates(query, settings, *, limit=5):
        return [
            _candidate("Parasite", imdb_id="tt6751668", year=2019),
            _candidate("Parasite", imdb_id="tt0084472", year=1982),
        ]

    settings = _settings_with_prefs(
        tmp_path,
        default_mode="manual",
        choice_style="telegram-rich",
    )
    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: settings)

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "Parasite"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "choose_title"
    assert payload["choices_table"] is None
    assert payload["choice_display"] == (
        "I found a few possible matches. Reply with the number of the title you mean:\n\n"
        "1. Parasite (2019)\n"
        "2. Parasite (1982)"
    )
    assert "```text" not in payload["choice_display"]
    assert "1. 1." not in payload["choice_display"]
    assert payload["ui_hints"] == {
        "choice_style": "telegram-rich",
        "recommended_button_layout": "inline-row",
        "closed_choice": True,
    }


def test_handle_keyword_choose_title_uses_title_choice_table(monkeypatch, tmp_path):
    async def fake_candidates(query, settings, *, limit=5):
        return [
            _candidate("Parasite", imdb_id="tt6751668", year=2019),
            _candidate("Parasite", imdb_id="tt0084472", year=1982),
        ]

    monkeypatch.setattr("mpilot.api.handle.search_movie_candidates", fake_candidates)
    monkeypatch.setattr("mpilot.api.handle.get_settings", lambda: _settings(tmp_path))

    client = TestClient(app)
    response = client.post("/handle", json={"user_message": "Parasite"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "choose_title"
    assert payload["choices_table"] == "1. Parasite (2019)\n2. Parasite (1982)"
    assert payload["choice_display"].startswith("I found a few possible matches.")
    assert "```text" in payload["choice_display"]
    assert len(payload["choice_buttons"]) == 2
    assert payload["results"] is None
    assert len(payload["candidates"]) == 2


def test_download_response_includes_prerendered_status(monkeypatch, tmp_path):
    from mpilot.acquisition.models import TorrentStatus

    async def fake_add_download(download_link, settings, *, save_path=None, requester_id=None):
        return TorrentStatus(
            name="Test.Movie.2026.1080p.BluRay-GRP",
            state="downloading",
            progress=0.04,
            size=2_100_000_000,
            seeds=1,
            hash="abcdef",
            download_speed=8_800_000,
            eta=625,
        )

    monkeypatch.setattr("mpilot.api.download.add_download_to_qbittorrent", fake_add_download)
    monkeypatch.setattr("mpilot.api.download.get_settings", lambda: _settings_with_prefs(tmp_path))

    client = TestClient(app)
    response = client.post(
        "/download",
        json={
            "download_link": "https://example.test/x.torrent",
            "user_id": "telegram:1",
            "save_path": "/downloads/movies",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    rendered = payload["rendered_status"]
    assert rendered is not None
    assert "🟩" in rendered or "⬜" in rendered  # 10-cell emoji bar
    assert "█" not in rendered and "░" not in rendered
    assert "💾" in rendered and "⚡" in rendered and "⏱️" in rendered
