from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from mpilot.acquisition.services.prowlarr import list_prowlarr_indexers, search_prowlarr
from mpilot.acquisition.models import SearchRequest


class FakeProwlarrResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FlakyAsyncClient:
    calls = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        type(self).calls += 1
        if type(self).calls == 1:
            request = httpx.Request("GET", url, params=params)
            raise httpx.ReadTimeout("slow prowlarr response", request=request)
        return FakeProwlarrResponse(
            [
                {
                    "title": "Jojo.Rabbit.2019.1080p.WEB-DL.H.264-GRP",
                    "downloadUrl": "/api/v1/indexer/1/download?link=abc",
                    "size": 100,
                    "seeders": 56,
                    "leechers": 1,
                    "indexer": "Indexer A",
                }
            ]
        )


def _settings():
    return SimpleNamespace(
        prowlarr_url="http://prowlarr.test",
        prowlarr_download_url=None,
        prowlarr_api_key="secret",
        request_timeout_seconds=30,
    )


def test_search_prowlarr_retries_once_after_read_timeout(monkeypatch):
    FlakyAsyncClient.calls = 0
    monkeypatch.setattr("mpilot.acquisition.services.prowlarr.httpx.AsyncClient", FlakyAsyncClient)

    results = asyncio.run(search_prowlarr(SearchRequest(query="tt2584384"), _settings()))

    assert FlakyAsyncClient.calls == 2
    assert len(results) == 1
    assert results[0].title == "Jojo.Rabbit.2019.1080p.WEB-DL.H.264-GRP"


class RecordingAsyncClient:
    calls = []
    payload = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        type(self).calls.append({"url": url, "params": params, "headers": headers})
        if url.endswith("/api/v1/indexer"):
            return FakeProwlarrResponse(type(self).payload)
        indexer_id = params["indexerIds"][0]
        return FakeProwlarrResponse(
            [
                {
                    "title": f"Result from {indexer_id}",
                    "downloadUrl": f"/api/v1/indexer/{indexer_id}/download?link=result",
                    "indexer": f"Indexer {indexer_id}",
                    "seeders": indexer_id,
                }
            ]
        )


def _routed_settings():
    return SimpleNamespace(
        prowlarr_url="http://prowlarr.test",
        prowlarr_download_url=None,
        prowlarr_api_key="secret",
        request_timeout_seconds=30,
        prowlarr_imdb_native_indexer_ids=[5, 6],
        prowlarr_imdb_keyword_indexer_ids=[4],
        prowlarr_imdb_disabled_indexer_ids=[1, 3],
        prowlarr_complementary_indexer_ids=[1, 9],
        imdb_indexer_routing_configured=True,
    )


def test_search_prowlarr_routes_exact_imdb_id_by_configured_indexer_mode(monkeypatch):
    RecordingAsyncClient.calls = []
    monkeypatch.setattr("mpilot.acquisition.services.prowlarr.httpx.AsyncClient", RecordingAsyncClient)

    results = asyncio.run(
        search_prowlarr(
            SearchRequest(query="tt7587282", categories=[2000, 5000]),
            _routed_settings(),
        )
    )

    assert len(results) == 2
    assert {result.indexer: result.source_search_mode for result in results} == {
        "Indexer 4": "keyword",
        "Indexer 5": "native",
    }
    params = [call["params"] for call in RecordingAsyncClient.calls]
    assert {
        (item["query"], item["type"], tuple(item["indexerIds"]), tuple(item["categories"]))
        for item in params
    } == {
        ("tt7587282", "search", (4,), (2000, 5000)),
        ("{ImdbId:tt7587282}", "movie", (5, 6), (2000, 5000)),
    }


def test_search_prowlarr_uses_tvsearch_for_canonical_tv_imdb(monkeypatch):
    RecordingAsyncClient.calls = []
    monkeypatch.setattr("mpilot.acquisition.services.prowlarr.httpx.AsyncClient", RecordingAsyncClient)

    asyncio.run(
        search_prowlarr(
            SearchRequest(query="tt7587282", categories=[5000], media_type="tv"),
            _routed_settings(),
        )
    )

    params = [call["params"] for call in RecordingAsyncClient.calls]
    assert {item["type"] for item in params} == {"search", "tvsearch"}
    assert all(item["categories"] == [5000] for item in params)


def test_search_prowlarr_intersects_imdb_modes_with_requested_indexers(monkeypatch):
    RecordingAsyncClient.calls = []
    monkeypatch.setattr("mpilot.acquisition.services.prowlarr.httpx.AsyncClient", RecordingAsyncClient)

    asyncio.run(
        search_prowlarr(
            SearchRequest(query="tt7587282", categories=[2000], indexer_ids=[5, 1, 99]),
            _routed_settings(),
        )
    )

    assert len(RecordingAsyncClient.calls) == 1
    assert RecordingAsyncClient.calls[0]["params"]["indexerIds"] == [5]
    assert RecordingAsyncClient.calls[0]["params"]["query"] == "{ImdbId:tt7587282}"


def test_list_prowlarr_indexers_summarizes_native_and_configured_modes(monkeypatch):
    RecordingAsyncClient.calls = []
    RecordingAsyncClient.payload = [
        {
            "id": 4,
            "name": "The Pirate Bay",
            "enable": True,
            "protocol": "torrent",
            "capabilities": {"movieSearchParams": ["q"]},
        },
        {
            "id": 6,
            "name": "Torrentio",
            "enable": True,
            "protocol": "torrent",
            "capabilities": {"movieSearchParams": ["q", "imdbId"]},
        },
        {
            "id": 9,
            "name": "New Indexer",
            "enable": True,
            "protocol": "torrent",
            "capabilities": {"movieSearchParams": ["q"]},
        },
    ]
    monkeypatch.setattr("mpilot.acquisition.services.prowlarr.httpx.AsyncClient", RecordingAsyncClient)

    indexers = asyncio.run(list_prowlarr_indexers(_routed_settings()))

    assert [(item.id, item.supports_imdb_parameter, item.imdb_search_mode) for item in indexers] == [
        (4, False, "keyword"),
        (6, True, "native"),
        (9, False, "unconfigured"),
    ]
    assert [(item.id, item.complementary_search_enabled) for item in indexers] == [
        (4, False),
        (6, False),
        (9, True),
    ]


class CategoryCompatibilityAsyncClient:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        type(self).calls.append({"url": url, "params": params, "headers": headers})
        if url.endswith("/api/v1/indexer"):
            return FakeProwlarrResponse(
                [
                    {
                        "id": 1,
                        "name": "HD Source",
                        "enable": True,
                        "capabilities": {
                            "categories": [
                                {"id": 2000, "subCategories": [{"id": 2040}]},
                                {"id": 5000, "subCategories": [{"id": 5040}]},
                            ]
                        },
                    },
                    *[
                        {
                            "id": indexer_id,
                            "name": name,
                            "enable": True,
                            "capabilities": {"categories": [{"id": 2000}, {"id": 5000}]},
                        }
                        for indexer_id, name in (
                            (3, "LimeTorrents"),
                            (6, "Torrentio"),
                            (7, "TorrentGalaxy"),
                            (9, "52BT"),
                        )
                    ],
                ]
            )

        indexer_ids = params["indexerIds"]
        if indexer_ids == [1]:
            return FakeProwlarrResponse(
                [
                    {
                        "title": "Port.Authority.2019.1080p.WEB-DL.H.264-HD",
                        "downloadUrl": "/api/v1/indexer/1/download?link=hd",
                        "indexer": "HD Source",
                    },
                    {
                        "title": "Port.Authority.2019.DVDRip.x264-MISCATEGORIZED",
                        "downloadUrl": "/api/v1/indexer/1/download?link=unknown",
                        "indexer": "HD Source",
                    },
                ]
            )
        return FakeProwlarrResponse(
            [
                {
                    "title": "中转站.Port Authority.2019.HD1080P.X264.AAC.English.CHS.mp4",
                    "downloadUrl": "/api/v1/indexer/9/download?link=1080",
                    "indexer": "52BT",
                },
                {
                    "title": "Port.Authority.2019.720p.BluRay.x264",
                    "downloadUrl": "/api/v1/indexer/9/download?link=720",
                    "indexer": "52BT",
                },
                {
                    "title": "Port.Authority.2019.DVDRip.x264",
                    "downloadUrl": "/api/v1/indexer/9/download?link=unknown",
                    "indexer": "52BT",
                },
            ]
        )


def test_hd_search_routes_parent_only_indexers_and_filters_their_results(monkeypatch):
    CategoryCompatibilityAsyncClient.calls = []
    monkeypatch.setattr(
        "mpilot.acquisition.services.prowlarr.httpx.AsyncClient",
        CategoryCompatibilityAsyncClient,
    )

    results = asyncio.run(
        search_prowlarr(
            SearchRequest(
                query="Port Authority 2019",
                categories=[2040, 5040],
                indexer_ids=[1, 3, 6, 7, 9],
                result_resolution="1080p",
            ),
            _settings(),
        )
    )

    searches = [call["params"] for call in CategoryCompatibilityAsyncClient.calls if call["params"]]
    assert {
        (tuple(params["indexerIds"]), tuple(params["categories"]))
        for params in searches
    } == {
        ((1,), (2040, 5040)),
        ((3, 6, 7, 9), (2000, 5000)),
    }
    assert [result.title for result in results] == [
        "Port.Authority.2019.1080p.WEB-DL.H.264-HD",
        "中转站.Port Authority.2019.HD1080P.X264.AAC.English.CHS.mp4",
    ]


class MixedMediaResultsAsyncClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        return FakeProwlarrResponse(
            [
                {
                    "title": "The.Count.Of.Monte.Cristo.2024.1080p.BluRay.x264",
                    "downloadUrl": "/api/v1/indexer/1/download?link=movie",
                    "categories": [{"id": 2040, "name": "Movies/HD"}],
                },
                {
                    "title": "The.Count.of.Monte.Cristo.2025.S01.1080p.WEB-DL.x264",
                    "downloadUrl": "/api/v1/indexer/1/download?link=tv-category",
                    "categories": [{"id": 5040, "name": "TV/HD"}],
                },
                {
                    "title": "The.Count.of.Monte.Cristo.2025.S01.1080p.WEB-DL.x265",
                    "downloadUrl": "/api/v1/indexer/1/download?link=tv-title",
                    "categories": [],
                },
                {
                    "title": "Le.Comte.de.Monte-Cristo.2024.1080p.WEB-DL.x264",
                    "downloadUrl": "/api/v1/indexer/1/download?link=uncategorized-movie",
                    "categories": [],
                },
            ]
        )


def test_canonical_movie_filter_rejects_tv_categories_and_season_titles(monkeypatch):
    monkeypatch.setattr(
        "mpilot.acquisition.services.prowlarr.httpx.AsyncClient",
        MixedMediaResultsAsyncClient,
    )

    results = asyncio.run(
        search_prowlarr(
            SearchRequest(query="tt26446278", categories=[2000], media_type="movie"),
            _settings(),
        )
    )

    assert [result.title for result in results] == [
        "The.Count.Of.Monte.Cristo.2024.1080p.BluRay.x264",
        "Le.Comte.de.Monte-Cristo.2024.1080p.WEB-DL.x264",
    ]
