from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from .matching import _artist_hit, art_norm, norm
from .navidrome import NavidromeSync

logger = logging.getLogger("app.playlist_import")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
QISHUI_HEADERS = {"User-Agent": UA, "Referer": "https://music.douyin.com/"}


def _proxies(proxy_url: str) -> dict[str, str] | None:
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def resolve_url(url: str) -> tuple[str, str, str]:
    """返回 (platform, id, 原始链接)。"""
    text = url.strip()
    lower = text.lower()

    if lower.startswith("spotify:playlist:"):
        return "spotify", text.split(":")[-1], text
    match = re.search(r"open\.spotify\.com/(?:intl-[a-z\-]+/)?playlist/([A-Za-z0-9]+)", text)
    if match:
        return "spotify", match.group(1), text

    match = re.search(r"music\.163\.com/(?:#/)?playlist\?id=(\d+)", text)
    if match:
        return "netease", match.group(1), text
    match = re.search(r"music\.163\.com/playlist/(\d+)", text)
    if match:
        return "netease", match.group(1), text

    match = re.search(r"y\.qq\.com/n/ryqq/playlist/(\d+)", text)
    if match:
        return "qq", match.group(1), text
    match = re.search(r"y\.qq\.com.*[?&]id=(\d+)", text)
    if match:
        return "qq", match.group(1), text
    match = re.search(r"i\.y\.qq\.com.*[?&]id=(\d+)", text)
    if match:
        return "qq", match.group(1), text

    match = re.search(
        r"music\.douyin\.com/qishui/(?:share/)?(chart|playlist)[/?]?(?:id=)?(\d{15,20})",
        text,
    )
    if match:
        return "qishui", f"{match.group(1)}:{match.group(2)}", text
    match = re.search(r"(?:qishui\.douyin\.com|music\.douyin\.com/qishui)/(?:s|share)/([A-Za-z0-9]+)", text)
    if match:
        return "qishui", f"short:{match.group(1)}", text

    if re.search(r"(163cn\.tv|c6\.y\.qq\.com/base/fcgi-bin/u)", lower):
        try:
            response = requests.get(text, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
            final = response.url
            logger.info("短链跳转: %s", final[:120])
            match = re.search(r"playlist/(\d+)|[?&]id=(\d+)", final)
            if match:
                value = match.group(1) or match.group(2)
                if "163" in final:
                    return "netease", value, final
                if "qq.com" in final:
                    return "qq", value, final
        except Exception as exc:
            logger.warning("短链解析失败: %s", exc)

    raise ValueError(f"无法识别的链接: {text}")


def fetch_netease(playlist_id: str) -> tuple[str, list[tuple[str, str]]]:
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    cookies = {"appver": "2.0.2", "os": "pc"}
    detail = requests.get(
        f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}&n=1000&s=0",
        headers=headers,
        cookies=cookies,
        timeout=20,
    )
    data = detail.json()
    playlist = data.get("playlist") or {}
    name = playlist.get("name") or f"网易云歌单{playlist_id}"
    tracks = playlist.get("tracks") or []
    output: list[tuple[str, str]] = []
    for track in tracks:
        artists = ", ".join(
            item.get("name", "") for item in (track.get("ar") or track.get("artists") or [])
        )
        output.append((track.get("name", ""), artists))

    if len(output) < (playlist.get("trackCount") or 0):
        try:
            response = requests.get(
                f"https://music.163.com/api/playlist/track/all?id={playlist_id}&limit=1000&offset=0",
                headers=headers,
                cookies=cookies,
                timeout=25,
            )
            songs = response.json().get("songs") or []
            if songs:
                output = [
                    (
                        song.get("name", ""),
                        ", ".join(
                            item.get("name", "")
                            for item in (song.get("ar") or song.get("artists") or [])
                        ),
                    )
                    for song in songs
                ]
        except Exception as exc:
            logger.warning("网易云全量补齐失败(用已抓到的 %s 首): %s", len(output), exc)
    return name, output


def fetch_qq(playlist_id: str) -> tuple[str, list[tuple[str, str]]]:
    url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    output: list[tuple[str, str]] = []
    name = f"QQ歌单{playlist_id}"
    begin, step = 0, 500
    while begin < 5000:
        payload = {
            "req_0": {
                "module": "music.srfDissInfo.aiDissInfo",
                "method": "uniform_get_Dissinfo",
                "param": {
                    "disstid": int(playlist_id),
                    "enc_host_uin": "",
                    "tag": 1,
                    "userinfo": 1,
                    "song_begin": begin,
                    "song_num": step,
                    "order": 1,
                },
            }
        }
        response = requests.post(
            url,
            data=json.dumps(payload),
            headers={
                "User-Agent": UA,
                "Referer": "https://y.qq.com/",
                "Content-Type": "application/json",
            },
            timeout=25,
        )
        data = (response.json().get("req_0") or {}).get("data") or {}
        info = data.get("dirinfo") or {}
        if begin == 0:
            name = info.get("title") or data.get("dissname") or name
        songs = data.get("songlist") or []
        if not songs:
            break
        for song in songs:
            singers = ", ".join(item.get("name", "") for item in (song.get("singer") or []))
            output.append((song.get("name", ""), singers))
        total = data.get("total_song_num") or 0
        begin += step
        if total and begin >= total:
            break
        if len(songs) < step:
            break
    return name, output


def _spotify_embed(playlist_id: str, proxies: dict[str, str] | None) -> tuple[str, list[tuple[str, str]]]:
    response = requests.get(
        f"https://open.spotify.com/embed/playlist/{playlist_id}",
        headers={"User-Agent": UA},
        proxies=proxies,
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"embed HTTP {response.status_code}")
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        raise RuntimeError("embed 页未解析到 __NEXT_DATA__")
    entity = json.loads(match.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    tracks = [
        (item.get("title", ""), item.get("subtitle", ""))
        for item in (entity.get("trackList") or [])
        if item.get("title")
    ]
    return entity.get("name") or f"Spotify歌单{playlist_id}", tracks


def _spotify_token() -> str:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("未配置 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET")

    token_file = Path(os.getenv("SPOTIFY_TOKEN_FILE", "/data/spotify_token.json"))
    try:
        if token_file.exists():
            cached = json.loads(token_file.read_text(encoding="utf-8"))
            if cached.get("expires", 0) > time.time() + 60:
                return cached["access_token"]
    except Exception:
        pass

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload["access_token"]
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(
            json.dumps({
                "access_token": token,
                "expires": time.time() + int(payload.get("expires_in", 3600)),
            }),
            encoding="utf-8",
        )
        os.chmod(token_file, 0o600)
    except Exception:
        pass
    return token


def fetch_spotify(
    playlist_id: str,
    proxy_url: str = "",
) -> tuple[str, list[tuple[str, str]]]:
    proxies = _proxies(proxy_url)
    try:
        name, tracks = _spotify_embed(playlist_id, proxies)
        logger.info("[spotify] embed 取到 %s 首", len(tracks))
        return name, tracks
    except Exception as exc:
        logger.warning("[spotify] embed 失败(%s)，回退 Web API", exc)

    token = _spotify_token()
    headers = {"Authorization": f"Bearer {token}"}
    meta = requests.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers=headers,
        params={"fields": "name,tracks.total"},
        proxies=proxies,
        timeout=25,
    ).json()
    name = meta.get("name") or f"Spotify歌单{playlist_id}"
    total = ((meta.get("tracks") or {}).get("total")) or 0
    output: list[tuple[str, str]] = []
    offset = 0
    while offset < max(total, 1):
        response = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            headers=headers,
            params={
                "limit": 100,
                "offset": offset,
                "fields": "items(track(name,artists(name)))",
            },
            proxies=proxies,
            timeout=25,
        )
        items = response.json().get("items") or []
        if not items:
            break
        for item in items:
            track = item.get("track") or item.get("item") or {}
            if not track or not track.get("name"):
                continue
            output.append((
                track.get("name", ""),
                ", ".join(artist.get("name", "") for artist in (track.get("artists") or [])),
            ))
        offset += 100
        if offset >= total:
            break
    return name, output


def _walk_tracks(value: Any, output: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        if value.get("name") and isinstance(value.get("artists"), list) and value["artists"] \
                and isinstance(value["artists"][0], dict):
            output.append((
                value["name"],
                ", ".join(item.get("name", "") for item in value["artists"]),
            ))
            return
        for child in value.values():
            _walk_tracks(child, output)
    elif isinstance(value, list):
        for child in value:
            _walk_tracks(child, output)


def fetch_qishui(spec: str, proxy_url: str = "") -> tuple[str, list[tuple[str, str]]]:
    kind, _, playlist_id = spec.partition(":")
    kwargs = {"proxies": _proxies(proxy_url)} if proxy_url else {}

    if kind == "short":
        response = requests.get(
            f"https://qishui.douyin.com/s/{playlist_id}",
            headers=QISHUI_HEADERS,
            timeout=20,
            allow_redirects=True,
            **kwargs,
        )
        final = response.url
        logger.info("[qishui] 短链跳转: %s", final[:120])
        match = re.search(r"(chart|playlist)[/?]?(?:id=)?(\d{15,20})", final)
        if match:
            kind, playlist_id = match.group(1), match.group(2)
        else:
            match = re.search(r'"(\d{15,20})"', response.text)
            if not match:
                raise RuntimeError(f"汽水短链未解析出 id: {final[:120]}")
            kind, playlist_id = "playlist", match.group(1)

    if kind == "chart":
        url = f"https://api.qishui.com/luna/pc/charts/{playlist_id}"
        data = requests.get(url, headers=QISHUI_HEADERS, timeout=30, **kwargs).json()
        name = ((data.get("chart") or {}).get("title")) or f"汽水榜单{playlist_id}"
    else:
        url = f"https://api.qishui.com/luna/pc/playlist/detail?playlist_id={playlist_id}"
        data = requests.get(url, headers=QISHUI_HEADERS, timeout=30, **kwargs).json()
        name = f"汽水歌单{playlist_id}"
        match = re.search(
            r'"(?:title|name)"\s*:\s*"([^"]{1,60})"',
            json.dumps(data, ensure_ascii=False),
        )
        if match:
            name = match.group(1)

    output: list[tuple[str, str]] = []
    _walk_tracks(data, output)
    seen, unique = set(), []
    for item in output:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    if not unique:
        raise RuntimeError(f"汽水接口未解析到曲目: {str(data)[:150]}")
    return name, unique


def fetch_playlist(url: str, proxy_url: str = "") -> tuple[str, str, str, list[tuple[str, str]]]:
    platform, playlist_id, _ = resolve_url(url)
    if platform == "netease":
        name, tracks = fetch_netease(playlist_id)
    elif platform == "qq":
        name, tracks = fetch_qq(playlist_id)
    elif platform == "qishui":
        name, tracks = fetch_qishui(playlist_id, proxy_url)
    else:
        name, tracks = fetch_spotify(playlist_id, proxy_url)
    return platform, playlist_id, name, tracks


def build_index(songs: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for song in songs:
        key = norm(song.get("title"))
        if key:
            index.setdefault(key, []).append(song)
    return index


def match_song(
    title: str,
    artist: str,
    index: dict[str, list[dict]],
    songs: list[dict],
    *,
    threshold: float = 0.86,
    loose: bool = False,
) -> dict | None:
    want_title = norm(title)
    want_artist = art_norm(artist)
    if not want_title:
        return None

    candidates = index.get(want_title, [])
    if candidates:
        for candidate in candidates:
            if _artist_hit(want_artist, art_norm(candidate.get("artist"))):
                return candidate
        return candidates[0] if loose else None

    from difflib import SequenceMatcher

    best: dict | None = None
    best_ratio = 0.0
    for song in songs:
        candidate_title = norm(song.get("title"))
        if not candidate_title:
            continue
        if not loose and want_artist and not _artist_hit(want_artist, art_norm(song.get("artist"))):
            continue
        ratio = SequenceMatcher(None, want_title, candidate_title).ratio()
        if ratio > best_ratio:
            best, best_ratio = song, ratio
    if best is not None and best_ratio >= threshold:
        return best
    return None


def _dedupe_tracks(tracks: list[dict], limit: int = 1000) -> tuple[list[dict], bool]:
    seen, unique = set(), []
    for track in tracks:
        key = (norm(track.get("title")), art_norm(track.get("artist")))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        unique.append(track)
    if len(unique) > limit:
        return unique[:limit], True
    return unique, False


def import_playlists(
    urls: list[str],
    navidrome: NavidromeSync,
    *,
    name: str | None = None,
    dry_run: bool = False,
    loose: bool = False,
    proxy_url: str = "",
    threshold: float = 0.86,
) -> dict[str, Any]:
    """抓取歌单、匹配本地曲库、创建 Navidrome 播放列表。"""
    songs = navidrome.all_songs()
    index = build_index(songs)
    logger.info("曲库载入: %s 首", len(songs))

    results: list[dict] = []
    all_missing: list[dict] = []

    for url in urls:
        item: dict[str, Any] = {"url": url}
        try:
            platform, playlist_id, original_name, tracks = fetch_playlist(url, proxy_url)
            list_name = name or original_name or f"{platform}歌单"
            matched_ids: list[str] = []
            missing: list[dict] = []

            for title, artist in tracks:
                song = match_song(
                    title, artist, index, songs,
                    threshold=threshold,
                    loose=loose,
                )
                if song and song.get("id"):
                    matched_ids.append(str(song["id"]))
                else:
                    missing.append({
                        "title": title,
                        "artist": artist,
                        "source": "netease",
                    })

            seen, unique_ids = set(), []
            for song_id in matched_ids:
                if song_id not in seen:
                    seen.add(song_id)
                    unique_ids.append(song_id)

            logger.info(
                "[%s] 《%s》抓取 %s 首 / 匹配 %s 首 / 未匹配 %s 首",
                platform, list_name, len(tracks), len(unique_ids), len(missing),
            )

            item.update({
                "platform": platform,
                "id": playlist_id,
                "name": list_name,
                "total": len(tracks),
                "matched": len(unique_ids),
                "missing": len(missing),
                "missing_tracks": missing[:200],
            })

            if not dry_run and unique_ids:
                existing = {
                    entry.get("name"): entry.get("id")
                    for entry in navidrome.playlists()
                }
                if list_name in existing:
                    navidrome.delete_playlist(str(existing[list_name]))
                    logger.info("[%s] 已删除同名旧列表 %s", platform, list_name)
                created = navidrome.create_playlist(list_name, unique_ids)
                playlist = created.get("playlist") or {}
                item["playlist_id"] = playlist.get("id")
                item["created"] = True
                logger.info("[%s] 已创建播放列表《%s》%s 首", platform, list_name, len(unique_ids))

            all_missing.extend(missing)
        except Exception as exc:
            logger.warning("[FAIL] %s -> %s", url, exc)
            item["error"] = str(exc)
        results.append(item)

    missing_tracks, truncated = _dedupe_tracks(all_missing)
    return {
        "library_songs": len(songs),
        "playlists": results,
        "missing_tracks": missing_tracks,
        "missing_truncated": truncated,
        "dry_run": dry_run,
    }
