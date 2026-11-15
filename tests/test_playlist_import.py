import pytest

from app import playlist_import
from app.playlist_import import import_playlists, match_song, resolve_url


class FakeNavidrome:
    def __init__(self, songs=None, playlists=None):
        self.songs = songs or []
        self._playlists = playlists or []
        self.created = []
        self.deleted = []

    def all_songs(self):
        return self.songs

    def playlists(self):
        return self._playlists

    def delete_playlist(self, playlist_id):
        self.deleted.append(playlist_id)

    def create_playlist(self, name, song_ids):
        self.created.append((name, song_ids))
        return {"playlist": {"id": "new-playlist"}}


@pytest.mark.parametrize(
    ("url", "platform", "playlist_id"),
    [
        ("https://music.163.com/playlist?id=3778678", "netease", "3778678"),
        ("https://y.qq.com/n/ryqq/playlist/123456", "qq", "123456"),
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "spotify", "37i9dQZF1DXcBWIGoYBM5M"),
        ("spotify:playlist:abc123", "spotify", "abc123"),
        ("https://music.douyin.com/qishui/playlist/7123456789012345678", "qishui", "playlist:7123456789012345678"),
    ],
)
def test_resolve_url(url, platform, playlist_id):
    assert resolve_url(url)[:2] == (platform, playlist_id)


def test_resolve_url_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_url("https://example.com/playlist/1")


def test_match_song_requires_artist_in_strict_mode():
    songs = [{"id": "1", "title": "小方桌", "artist": "别人"}]
    index = {"小方桌": songs}
    assert match_song("小方桌", "小春", index, songs) is None
    assert match_song("小方桌", "小春", index, songs, loose=True)["id"] == "1"


def test_import_playlists_creates_playlist_and_reports_missing(monkeypatch):
    nav = FakeNavidrome(
        songs=[{"id": "1", "title": "小方桌", "artist": "小春"}],
        playlists=[{"name": "测试歌单", "id": "old-playlist"}],
    )

    def fake_fetch(url, proxy_url=""):
        return "netease", "123", "测试歌单", [("小方桌", "小春"), ("温柔", "五月天")]

    monkeypatch.setattr(playlist_import, "fetch_playlist", fake_fetch)

    result = import_playlists(["https://music.163.com/playlist?id=123"], nav)

    assert nav.deleted == ["old-playlist"]
    assert nav.created == [("测试歌单", ["1"])]
    assert result["playlists"][0]["matched"] == 1
    assert result["playlists"][0]["missing"] == 1
    assert result["missing_tracks"][0]["title"] == "温柔"


def test_import_playlists_dry_run_does_not_write(monkeypatch):
    nav = FakeNavidrome(songs=[{"id": "1", "title": "小方桌", "artist": "小春"}])
    monkeypatch.setattr(
        playlist_import,
        "fetch_playlist",
        lambda url, proxy_url="": ("netease", "123", "测试歌单", [("小方桌", "小春")]),
    )

    result = import_playlists(
        ["https://music.163.com/playlist?id=123"], nav, dry_run=True
    )

    assert result["playlists"][0]["matched"] == 1
    assert nav.created == []
    assert nav.deleted == []
