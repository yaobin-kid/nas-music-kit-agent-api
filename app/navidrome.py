from __future__ import annotations

import hashlib
import secrets

import httpx


class NavidromeError(Exception):
    """Navidrome / Subsonic API 调用失败。"""


class NavidromeClient:
    """触发 Navidrome 扫描目录。"""

    def __init__(
        self,
        base_url: str,
        *,
        user: str,
        password: str,
        client_name: str = "nas-music-kit",
        api_version: str = "1.16.1",
        timeout: float = 30.0,
        scan_timeout: float = 300.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout),
        )
        self._user = user
        self._password = password
        self._client_name = client_name
        self._api_version = api_version
        self._scan_timeout = scan_timeout

    async def aclose(self) -> None:
        await self._client.aclose()

    def _auth_params(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self._user,
            "t": token,
            "s": salt,
            "v": self._api_version,
            "c": self._client_name,
            "f": "json",
        }

    async def _get(self, method: str, extra: dict[str, str] | None = None) -> dict:
        params = self._auth_params()
        if extra:
            params.update(extra)
        response = await self._client.get(f"/rest/{method}.view", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise NavidromeError(f"{method} 返回非 JSON 响应: HTTP {response.status_code}") from exc

        data = payload.get("subsonic-response") or {}
        if data.get("status") != "ok":
            raise NavidromeError(f"{method} 失败: {data.get('error') or payload}")
        return data

    async def start_scan(self, wait: bool = True) -> dict:
        data = await self._get("startScan")
        result: dict = {"started": True, "waited": False, "timed_out": False}

        if not wait:
            return result

        import asyncio
        import time

        deadline = time.monotonic() + self._scan_timeout
        while time.monotonic() < deadline:
            status = await self._get("getScanStatus")
            scan_status = status.get("scanStatus") or {}
            if not scan_status.get("scanning", False):
                result["waited"] = True
                result["count"] = scan_status.get("count")
                return result
            await asyncio.sleep(2)

        result["timed_out"] = True
        return result
