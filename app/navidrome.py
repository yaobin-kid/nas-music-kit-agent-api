from __future__ import annotations

import hashlib
import secrets

import httpx
import requests


class NavidromeError(Exception):
    """Navidrome / Subsonic API 调用失败。"""


def _auth_params(user: str, password: str, client_name: str, api_version: str) -> dict[str, str]:
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": api_version,
        "c": client_name,
        "f": "json",
    }


class NavidromeSync:
    """同步版 Navidrome 客户端，供歌单导入线程使用。"""

    def __init__(
        self,
        base_url: str,
        *,
        user: str,
        password: str,
        client_name: str = "nas-music-kit",
        api_version: str = "1.16.1",
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._user = user
        self._password = password
        self._client_name = client_name
        self._api_version = api_version
        self._timeout = timeout
        self._session = requests.Session()

    def _get(self, method: str, pairs: list[tuple[str, str]] | None = None) -> dict:
        params = list(_auth_params(
            self._user, self._password, self._client_name, self._api_version
        ).items()) + list(pairs or [])
        try:
            response = self._session.get(
                f"{self._base_url}/rest/{method}.view",
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise NavidromeError(f"Navidrome 请求失败: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise NavidromeError(f"{method} 返回非 JSON 响应: HTTP {response.status_code}") from exc

        data = payload.get("subsonic-response") or {}
        if data.get("status") != "ok":
            raise NavidromeError(f"{method} 失败: {data.get('error') or payload}")
        return data

    def _post(self, method: str, pairs: list[tuple[str, str]]) -> dict:
        data = dict(_auth_params(
            self._user, self._password, self._client_name, self._api_version
        ))
        payload = []
        for key, value in pairs:
            payload.append((key, value))
        try:
            response = self._session.post(
                f"{self._base_url}/rest/{method}.view",
                data=list(data.items()) + payload,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise NavidromeError(f"Navidrome 请求失败: {exc}") from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise NavidromeError(f"{method} 返回非 JSON 响应: HTTP {response.status_code}") from exc

        body = result.get("subsonic-response") or {}
        if body.get("status") != "ok":
            raise NavidromeError(f"{method} 失败: {body.get('error') or result}")
        return body

    def all_songs(self) -> list[dict]:
        songs: list[dict] = []
        offset = 0
        while offset < 50000:
            data = self._get("search3", [
                ("query", ""),
                ("songCount", "5000"),
                ("songOffset", str(offset)),
                ("artistCount", "0"),
                ("albumCount", "0"),
            ])
            batch = data.get("searchResult3", {}).get("song", []) or []
            if isinstance(batch, dict):
                batch = [batch]
            songs.extend(batch)
            if len(batch) < 5000:
                break
            offset += 5000
        return songs

    def playlists(self) -> list[dict]:
        data = self._get("getPlaylists")
        items = data.get("playlists", {}).get("playlist", []) or []
        if isinstance(items, dict):
            items = [items]
        return items

    def delete_playlist(self, playlist_id: str) -> None:
        self._get("deletePlaylist", [("id", playlist_id)])

    def create_playlist(self, name: str, song_ids: list[str]) -> dict:
        pairs = [("name", name)] + [("songId", sid) for sid in song_ids]
        return self._post("createPlaylist", pairs)


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
