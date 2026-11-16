import json
import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app

BASE = "https://music.example.com"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("MUSIC_API_RETRIES", "0")
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_charts_status_and_refresh_without_config(client):
    state = client.get("/api/charts/status")
    assert state.status_code == 200
    assert state.json()["configured"] is False

    refresh = client.post("/api/charts/refresh", json={"dry_run": True})
    assert refresh.status_code == 503


def test_charts_refresh_endpoint(monkeypatch, tmp_path):
    charts_dir = tmp_path / "charts"
    charts_dir.mkdir()
    (charts_dir / "charts.py").write_text(
        "print('[agent] 已提交补歌任务 abcdef1234567890，共 2 首')\n",
        encoding="utf-8",
    )
    config = charts_dir / "config.json"
    config.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTS_DIR", str(charts_dir))
    monkeypatch.setenv("CHARTS_CONFIG", str(config))
    monkeypatch.setenv("CHARTS_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CHARTS_STATE_FILE", str(tmp_path / "charts-state.json"))
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/charts/refresh?wait=true", json={"dry_run": True}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["job_ids"] == ["abcdef1234567890"]


@pytest.fixture(autouse=True)
def _clear_access_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_CIDRS", "")
    monkeypatch.setenv("ENABLE_IP_ALLOWLIST", "auto")
    monkeypatch.setenv("AUTH_USER", "")
    monkeypatch.setenv("AUTH_PASSWORD", "")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")


def test_dashboard_status(client, monkeypatch):
    async def fake_probe(self):
        return {"healthy": True, "status_code": 200, "latency_ms": 1.0, "error": None}

    monkeypatch.setattr("app.client.MusicApiClient.probe", fake_probe)
    response = client.get("/api/dashboard/status")

    assert response.status_code == 200
    body = response.json()
    assert body["upstream"]["healthy"] is True
    assert body["upstream"]["base_url"] == BASE
    assert body["jobs"]["counts"]["running"] == 0
    assert body["charts"]["configured"] is False
    assert body["library"]["download_dir"] == body["library"]["library_dir"]


def test_dashboard_page(client):
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "监控大盘" in response.text
    assert "快捷操作" in response.text


def test_cidr_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOWED_CIDRS", "192.168.1.0/24")

    with TestClient(app, client=("192.168.1.20", 1000)) as test_client:
        assert test_client.get("/healthz").status_code == 200
        assert test_client.get("/dashboard").status_code == 200

    with TestClient(app, client=("10.0.0.5", 1000)) as test_client:
        assert test_client.get("/healthz").status_code == 200
        assert test_client.get("/dashboard").status_code == 403
        assert test_client.get("/api/dashboard/status").status_code == 403
        assert test_client.get("/api/charts/status").status_code == 200


def test_ip_allowlist_switch_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOWED_CIDRS", "192.168.1.0/24")
    monkeypatch.setenv("ENABLE_IP_ALLOWLIST", "false")

    with TestClient(app, client=("10.0.0.5", 1000)) as test_client:
        assert test_client.get("/dashboard").status_code == 200


def test_ip_allowlist_enabled_requires_cidrs(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOWED_CIDRS", "")
    monkeypatch.setenv("ENABLE_IP_ALLOWLIST", "true")

    with pytest.raises(RuntimeError, match="ALLOWED_CIDRS"):
        with TestClient(app):
            pass


def test_trust_proxy_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOWED_CIDRS", "192.168.1.0/24")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")

    with TestClient(app, client=("127.0.0.1", 1000)) as test_client:
        response = test_client.get(
            "/dashboard",
            headers={"X-Forwarded-For": "192.168.1.30"},
        )
        assert response.status_code == 200


def test_basic_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_USER", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", "secret")

    with TestClient(app) as test_client:
        assert test_client.get("/healthz").status_code == 200
        assert test_client.get("/api/charts/status").status_code == 200

        unauthorized = test_client.get("/dashboard")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"].startswith("Basic")

        assert test_client.get(
            "/dashboard", auth=("admin", "secret")
        ).status_code == 200
        assert test_client.get(
            "/dashboard", auth=("admin", "wrong")
        ).status_code == 401


def test_auth_requires_both_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_USER", "admin")

    with pytest.raises(RuntimeError, match="AUTH_USER"):
        with TestClient(app):
            pass


def test_parse_networks_rejects_invalid_value():
    from app.security import parse_networks

    with pytest.raises(ValueError, match="ALLOWED_CIDRS"):
        parse_networks("not-a-cidr")


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


def test_playlist_import_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
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
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    (tmp_path / "track.mp3").write_bytes(b"not really audio")

    with TestClient(app) as test_client:
        response = test_client.post("/api/library/organize")

    assert response.status_code == 200
    assert response.json()["scanned"] == 1


@respx.mock
def test_local_rate_limit_returns_429(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("MUSIC_API_RETRIES", "0")
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
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
def test_upstream_429_keeps_retry_after(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_API_BASE_URL", BASE)
    monkeypatch.setenv("MUSIC_API_RETRIES", "0")
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))

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
