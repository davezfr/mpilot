from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from app.services.prowlarr import search_prowlarr
from app.models import SearchRequest


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
    monkeypatch.setattr("app.services.prowlarr.httpx.AsyncClient", FlakyAsyncClient)

    results = asyncio.run(search_prowlarr(SearchRequest(query="tt2584384"), _settings()))

    assert FlakyAsyncClient.calls == 2
    assert len(results) == 1
    assert results[0].title == "Jojo.Rabbit.2019.1080p.WEB-DL.H.264-GRP"
