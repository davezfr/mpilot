from __future__ import annotations

from typing import Any

import httpx

from mpilot.acquisition.env import env_first


DEFAULT_QBITLARR_API_URL = "http://127.0.0.1:8000"


class QbitlarrApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class QbitlarrApiClient:
    def __init__(
        self,
        api_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.timeout = timeout
        self.transport = transport

    async def search(
        self,
        *,
        identifier: str | None = None,
        query: str | None = None,
        categories: list[int] | None = None,
        indexer_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "POST",
            "/search",
            json={
                "identifier": identifier,
                "query": query,
                "categories": categories,
                "indexer_ids": indexer_ids,
            },
        )
        if not isinstance(response, list):
            raise QbitlarrApiError("qBitlarr API returned an unexpected search response")
        return response

    async def download(
        self,
        download_link: str,
        save_path: str | None = None,
        query_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/download",
            json={
                "download_link": download_link,
                "save_path": save_path,
                "query_id": query_id,
                "user_id": user_id,
            },
        )
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected download response")
        return response

    async def handle(
        self,
        user_message: str,
        user_id: str | None = None,
        save_path: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/handle",
            json={
                "user_message": user_message,
                "user_id": user_id,
                "save_path": save_path,
                "mode": mode,
            },
        )
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected handle response")
        return response

    async def health(self, *, deep: bool = False) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if deep:
            kwargs["params"] = {"deep": "true"}
        response = await self._request("GET", "/health", **kwargs)
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected health response")
        return response

    async def list_downloads(self, user_id: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if user_id:
            kwargs["params"] = {"user_id": user_id}
        response = await self._request("GET", "/downloads", **kwargs)
        if not isinstance(response, list):
            raise QbitlarrApiError("qBitlarr API returned an unexpected downloads response")
        return response

    async def get_download_status(self, info_hash: str, user_id: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if user_id:
            kwargs["params"] = {"user_id": user_id}
        response = await self._request("GET", f"/downloads/{info_hash}", **kwargs)
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected download status response")
        return response

    async def render_downloads_status(self, user_id: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if user_id:
            kwargs["params"] = {"user_id": user_id}
        response = await self._request("GET", "/downloads/status-message", **kwargs)
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected rendered downloads response")
        return response

    async def render_download_status(self, info_hash: str, user_id: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if user_id:
            kwargs["params"] = {"user_id": user_id}
        response = await self._request("GET", f"/downloads/{info_hash}/status-message", **kwargs)
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected rendered download status response")
        return response

    async def pause_download(self, info_hash: str, user_id: str) -> dict[str, Any]:
        return await self._control_download(info_hash, user_id=user_id, action="pause")

    async def resume_download(self, info_hash: str, user_id: str) -> dict[str, Any]:
        return await self._control_download(info_hash, user_id=user_id, action="resume")

    async def delete_download(self, info_hash: str, user_id: str) -> dict[str, Any]:
        return await self._control_download(info_hash, user_id=user_id, action="delete")

    async def _control_download(self, info_hash: str, *, user_id: str, action: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/downloads/{info_hash}/{action}",
            params={"user_id": user_id},
        )
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected download control response")
        return response

    async def get_query_snapshot(self, query_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/queries/{query_id}")
        if not isinstance(response, dict):
            raise QbitlarrApiError("qBitlarr API returned an unexpected query snapshot response")
        return response

    async def list_prowlarr_indexers(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/prowlarr/indexers")
        if not isinstance(response, list):
            raise QbitlarrApiError("qBitlarr API returned an unexpected Prowlarr indexer response")
        return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        async with httpx.AsyncClient(
            base_url=self.api_url,
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            try:
                response = await client.request(method, path, headers=headers, **kwargs)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise QbitlarrApiError(
                    _extract_error_detail(exc.response),
                    status_code=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                raise QbitlarrApiError(f"qBitlarr API is unreachable: {exc.__class__.__name__}") from exc

        try:
            return response.json()
        except ValueError as exc:
            raise QbitlarrApiError("qBitlarr API returned invalid JSON") from exc


def get_qbitlarr_client() -> QbitlarrApiClient:
    return QbitlarrApiClient(
        api_url=env_first("QBITLARR_API_URL", default=DEFAULT_QBITLARR_API_URL) or DEFAULT_QBITLARR_API_URL,
        api_key=env_first("QBITLARR_API_KEY"),
        timeout=float(env_first("QBITLARR_API_TIMEOUT_SECONDS", default="90") or "90"),
    )


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"qBitlarr API returned HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()

    return f"qBitlarr API returned HTTP {response.status_code}"
