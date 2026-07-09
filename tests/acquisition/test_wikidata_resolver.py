from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from mpilot.acquisition.domain.quality import extract_external_movie_id
from mpilot.acquisition.services.wikidata import resolve_external_movie_id, search_movie_candidates


class FakeWikidataResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    requests: list[dict] = []
    payload = {"results": {"bindings": []}}

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        return FakeWikidataResponse(self.payload)


def _settings():
    return SimpleNamespace(request_timeout_seconds=30)


def _reset_fakes():
    FakeAsyncClient.requests = []
    FakeAsyncClient.payload = {"results": {"bindings": []}}


def test_extract_external_movie_id_from_douban_subject_url():
    assert extract_external_movie_id("https://movie.douban.com/subject/1292052/") == {
        "source": "douban",
        "source_id": "1292052",
    }


def test_extract_external_movie_id_from_allocine_film_url():
    assert extract_external_movie_id("https://www.allocine.fr/film/fichefilm_gen_cfilm=25801.html") == {
        "source": "allocine",
        "source_id": "25801",
    }


def test_extract_external_movie_id_accepts_prefixed_ids():
    assert extract_external_movie_id("douban:1292052") == {
        "source": "douban",
        "source_id": "1292052",
    }
    assert extract_external_movie_id("allocine:25801") == {
        "source": "allocine",
        "source_id": "25801",
    }


def test_resolve_external_movie_id_maps_douban_to_imdb_via_wikidata(monkeypatch):
    _reset_fakes()
    FakeAsyncClient.payload = {
        "results": {
            "bindings": [
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q172241"},
                    "imdb": {"value": "tt0111161"},
                }
            ]
        }
    }

    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(resolve_external_movie_id("https://movie.douban.com/subject/1292052/", _settings()))

    assert result == {
        "source": "douban",
        "source_id": "1292052",
        "imdb_id": "tt0111161",
        "wikidata_qid": "Q172241",
    }
    assert FakeAsyncClient.requests
    assert "wd:Q11424" in FakeAsyncClient.requests[0]["params"]["query"]


def test_resolve_external_movie_id_returns_unresolved_for_known_allocine_input_without_match(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        resolve_external_movie_id("https://www.allocine.fr/film/fichefilm_gen_cfilm=25801.html", _settings())
    )

    assert result == {
        "source": "allocine",
        "source_id": "25801",
        "imdb_id": None,
        "wikidata_qid": None,
    }


def test_resolve_external_movie_id_returns_unresolved_for_known_allocine_series_url():
    result = asyncio.run(
        resolve_external_movie_id("https://www.allocine.fr/series/ficheserie_gen_cserie=543.html", _settings())
    )

    assert result == {
        "source": "allocine",
        "source_id": None,
        "imdb_id": None,
        "wikidata_qid": None,
    }


def test_resolve_external_movie_id_returns_none_for_non_external_input():
    assert asyncio.run(resolve_external_movie_id("The Hitch-Hiker", _settings())) is None


class FakeErrorClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        raise httpx.RequestError("boom")


class FakeSequenceClient:
    payloads: list = []
    requests: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, params=None, headers=None):
        FakeSequenceClient.requests.append({"params": params})
        payload = FakeSequenceClient.payloads.pop(0) if FakeSequenceClient.payloads else {"results": {"bindings": []}}
        return FakeWikidataResponse(payload)


def _candidate_binding(*, qid, label, imdb, year=None, ordinal=0):
    binding = {
        "item": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "itemLabel": {"value": label},
        "imdb": {"value": imdb},
        "ordinal": {"value": str(ordinal)},
    }
    if year is not None:
        binding["year"] = {"value": str(year)}
    return binding


def test_search_movie_candidates_returns_ranked_unique_films(monkeypatch):
    _reset_fakes()
    FakeAsyncClient.payload = {
        "results": {
            "bindings": [
                _candidate_binding(qid="Q172241", label="The Shawshank Redemption", imdb="tt0111161", year=1994, ordinal=0),
                # Same film, second publication date - must be deduped to the first row.
                _candidate_binding(qid="Q172241", label="The Shawshank Redemption", imdb="tt0111161", year=1995, ordinal=0),
                _candidate_binding(qid="Q47703", label="The Godfather", imdb="tt0068646", year=1972, ordinal=1),
            ]
        }
    }
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(search_movie_candidates("shawshank", _settings()))

    assert result == [
        {"title": "The Shawshank Redemption", "year": 1994, "imdb_id": "tt0111161", "wikidata_qid": "Q172241"},
        {"title": "The Godfather", "year": 1972, "imdb_id": "tt0068646", "wikidata_qid": "Q47703"},
    ]


def test_search_movie_candidates_filters_non_title_imdb_ids(monkeypatch):
    _reset_fakes()
    FakeAsyncClient.payload = {
        "results": {
            "bindings": [
                _candidate_binding(qid="Q5", label="Some Director", imdb="nm0000123", ordinal=0),
                _candidate_binding(qid="Q172241", label="The Shawshank Redemption", imdb="tt0111161", year=1994, ordinal=1),
            ]
        }
    }
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(search_movie_candidates("shawshank", _settings()))

    assert [c["imdb_id"] for c in result] == ["tt0111161"]


def test_search_movie_candidates_skips_rows_without_a_real_label(monkeypatch):
    _reset_fakes()
    FakeAsyncClient.payload = {
        "results": {
            "bindings": [
                # No label in the requested language: the label service echoes the QID.
                _candidate_binding(qid="Q999999", label="Q999999", imdb="tt0000001", ordinal=0),
                _candidate_binding(qid="Q47703", label="The Godfather", imdb="tt0068646", year=1972, ordinal=1),
            ]
        }
    }
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(search_movie_candidates("godfather", _settings()))

    assert [c["imdb_id"] for c in result] == ["tt0068646"]


def test_search_movie_candidates_respects_limit(monkeypatch):
    _reset_fakes()
    FakeAsyncClient.payload = {
        "results": {
            "bindings": [
                _candidate_binding(qid="Q1", label="Film One", imdb="tt0000001", ordinal=0),
                _candidate_binding(qid="Q2", label="Film Two", imdb="tt0000002", ordinal=1),
                _candidate_binding(qid="Q3", label="Film Three", imdb="tt0000003", ordinal=2),
            ]
        }
    }
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(search_movie_candidates("film", _settings(), limit=2))

    assert [c["imdb_id"] for c in result] == ["tt0000001", "tt0000002"]


def test_search_movie_candidates_returns_empty_when_no_results(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    assert asyncio.run(search_movie_candidates("nothing matches this", _settings())) == []


def test_search_movie_candidates_returns_empty_on_upstream_error(monkeypatch):
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeErrorClient)

    assert asyncio.run(search_movie_candidates("shawshank", _settings())) == []


def test_search_movie_candidates_skips_request_for_blank_query(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    assert asyncio.run(search_movie_candidates("   ", _settings())) == []
    assert FakeAsyncClient.requests == []


def test_search_movie_candidates_query_uses_entitysearch_and_escapes_quotes(monkeypatch):
    _reset_fakes()
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(search_movie_candidates('The "Best" Movie', _settings()))

    assert FakeAsyncClient.requests
    query = FakeAsyncClient.requests[0]["params"]["query"]
    assert "EntitySearch" in query
    assert 'STRSTARTS(?imdb, "tt")' in query
    assert "wdt:P31/wdt:P279*" in query
    assert '\\"Best\\"' in query


def test_search_movie_candidates_retries_without_trailing_year(monkeypatch):
    FakeSequenceClient.requests = []
    FakeSequenceClient.payloads = [
        {"results": {"bindings": []}},
        {
            "results": {
                "bindings": [
                    _candidate_binding(qid="Q1815834", label="The Hitch-Hiker", imdb="tt0045877", year=1953, ordinal=0),
                ]
            }
        },
    ]
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeSequenceClient)

    result = asyncio.run(search_movie_candidates("The Hitch-Hiker 1953", _settings()))

    assert [c["imdb_id"] for c in result] == ["tt0045877"]
    assert len(FakeSequenceClient.requests) == 2
    assert "1953" in FakeSequenceClient.requests[0]["params"]["query"]
    assert "1953" not in FakeSequenceClient.requests[1]["params"]["query"]


def test_search_movie_candidates_does_not_retry_without_a_trailing_year(monkeypatch):
    FakeSequenceClient.requests = []
    FakeSequenceClient.payloads = [{"results": {"bindings": []}}]
    monkeypatch.setattr("mpilot.acquisition.services.wikidata.httpx.AsyncClient", FakeSequenceClient)

    result = asyncio.run(search_movie_candidates("totally unknown thing", _settings()))

    assert result == []
    assert len(FakeSequenceClient.requests) == 1
