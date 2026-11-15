from __future__ import annotations

import asyncio
from typing import Any

import httpx


class MusicApiError(Exception):
    """上游音乐 API 调用失败。"""

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int | None = None,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status
        self.response_body = response_body


class MusicApiClient:
    """封装 NAS 音乐 API 的 /api/search 与 /api/download。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        retries: int = 2,
        cookie: str = "",
        bearer_token: str = "",
        user_agent: str = "nas-music-kit-agent-api",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._retries = max(0, retries)

        headers = {
            "Accept": "application/json",
            "User-Agent": user_agent,
        }
        if cookie:
            headers["Cookie"] = cookie
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        last_status: int | None = None
        last_body = ""

        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if attempt < self._retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise MusicApiError(f"请求音乐 API 失败: {exc}") from exc

            if response.status_code >= 500:
                last_status = response.status_code
                last_body = response.text[:500]
                if attempt < self._retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise MusicApiError(
                    "音乐 API 暂时不可用",
                    upstream_status=last_status,
                    response_body=last_body,
                )

            if response.status_code >= 400:
                raise MusicApiError(
                    f"音乐 API 返回错误: HTTP {response.status_code}",
                    upstream_status=response.status_code,
                    response_body=response.text[:500],
                )

            try:
                return response.json()
            except ValueError as exc:
                raise MusicApiError(
                    "音乐 API 返回了非 JSON 响应",
                    upstream_status=response.status_code,
                    response_body=response.text[:500],
                ) from exc

        raise MusicApiError(
            "音乐 API 请求失败",
            upstream_status=last_status,
            response_body=last_body,
        )

    async def search(self, *, source: str, name: str, pages: int = 1) -> Any:
        return await self._request(
            "GET",
            "/api/search",
            params={"source": source, "name": name, "pages": pages},
        )

    async def download(self, payload: dict[str, Any]) -> Any:
        return await self._request("POST", "/api/download", json=payload)
