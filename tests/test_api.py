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
