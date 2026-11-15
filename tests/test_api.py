import json
import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app

BASE = "https://music.example.com"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("MUSIC_API_RETRIES", "0")
    with TestClient(app) as test_client:
        yield test_client


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@respx.mock
def test_search_forwards_params(client):
    route = respx.get(url__regex=rf"{re.escape(BASE)}/api/search.*").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "3412527335",
                    "name": "掉漆小方桌",
                    "artist": ["小春"],
                    "source": "netease",
                }
            ],
        )
    )

    response = client.get(
        "/api/search",
        params={"source": "netease", "name": "小方桌", "pages": 1},
    )

    assert response.status_code == 200
    assert response.json()[0]["id"] == "3412527335"
    request = route.calls[0].request
    assert request.url.params["source"] == "netease"
    assert request.url.params["name"] == "小方桌"
    assert request.url.params["pages"] == "1"


@respx.mock
def test_download_forwards_payload(client):
    route = respx.post(f"{BASE}/api/download").mock(
        return_value=httpx.Response(
            200,
            json={
                "bitrate": "999",
                "filename": "小春 - 掉漆小方桌.mp3",
                "status": "success",
                "tags": "ok",
            },
        )
    )

    payload = {
        "source": "netease",
        "id": "3412527335",
        "name": "掉漆小方桌",
        "artist": "小春",
        "album": "",
        "pic_id": "109951173651495877",
        "br": "999",
        "lyric": True,
        "vip": False,
    }

    response = client.post("/api/download", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    sent = json.loads(route.calls[0].request.content)
    assert sent == payload


@respx.mock
def test_upstream_error_becomes_502(client):
    respx.get(url__regex=rf"{re.escape(BASE)}/api/search.*").mock(
        return_value=httpx.Response(503, text="service unavailable")
    )

    response = client.get(
        "/api/search",
        params={"source": "netease", "name": "小方桌"},
    )

    assert response.status_code == 502
    assert response.json()["upstream_status"] == 503


def test_search_requires_name(client):
    response = client.get("/api/search", params={"source": "netease"})
    assert response.status_code == 422


def test_unknown_job_returns_404(client):
    response = client.get("/api/jobs/not-a-job")
    assert response.status_code == 404


def test_navidrome_scan_requires_configuration(client):
    response = client.post("/api/navidrome/scan")
    assert response.status_code == 503


def test_playlist_import_requires_navidrome(client):
    response = client.post(
        "/api/playlist/import",
        json={"urls": ["https://music.163.com/playlist?id=1"]},
    )
    assert response.status_code == 503


class _FakeSyncNavidrome:
    def __init__(self):
        self.created = []

    def all_songs(self):
        return [{"id": "1", "title": "小方桌", "artist": "小春"}]

    def playlists(self):
        return []

    def delete_playlist(self, playlist_id):
        pass

    def create_playlist(self, name, song_ids):
        self.created.append((name, song_ids))
        return {"playlist": {"id": "list-1"}}


def test_playlist_import_endpoint(monkeypatch):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    fake = _FakeSyncNavidrome()
    monkeypatch.setattr(
        "app.playlist_import.fetch_playlist",
        lambda url, proxy_url="": ("netease", "123", "测试歌单", [("小方桌", "小春")]),
    )

    with TestClient(app) as test_client:
        test_client.app.state.navidrome_sync = fake
        response = test_client.post(
            "/api/playlist/import",
            json={
                "urls": ["https://music.163.com/playlist?id=123"],
                "backfill": False,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["playlists"][0]["matched"] == 1
    assert body["playlists"][0]["playlist_id"] == "list-1"
    assert fake.created == [("测试歌单", ["1"])]


def test_organize_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    (tmp_path / "track.mp3").write_bytes(b"not really audio")

    with TestClient(app) as test_client:
        response = test_client.post("/api/library/organize")

    assert response.status_code == 200
    assert response.json()["scanned"] == 1


@respx.mock
def test_local_rate_limit_returns_429(monkeypatch):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("MUSIC_API_RETRIES", "0")
    monkeypatch.setenv("MUSIC_API_RATE_LIMIT", "1")
    monkeypatch.setenv("MUSIC_API_RATE_WINDOW", "300")

    respx.get(url__regex=rf"{re.escape(BASE)}/api/search.*").mock(
        return_value=httpx.Response(200, json=[])
    )

    with TestClient(app) as test_client:
        params = {"source": "netease", "name": "小方桌", "pages": 1}
        assert test_client.get("/api/search", params=params).status_code == 200
        limited = test_client.get("/api/search", params=params)

    assert limited.status_code == 429
    assert 1 <= int(limited.headers["retry-after"]) <= 300


@respx.mock
def test_upstream_429_keeps_retry_after(monkeypatch):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("MUSIC_API_RETRIES", "0")

    respx.get(url__regex=rf"{re.escape(BASE)}/api/search.*").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"error": "rate limited"},
        )
    )

    with TestClient(app) as test_client:
        response = test_client.get(
            "/api/search",
            params={"source": "netease", "name": "小方桌"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
