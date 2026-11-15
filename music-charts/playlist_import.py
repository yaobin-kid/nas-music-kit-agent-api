#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部歌单导入：QQ音乐 / 网易云 / Spotify 歌单链接 -> Navidrome 播放列表

用法:
  python3 playlist_import.py <链接1> [<链接2> ...] [--name 自定义名] [--dry-run]

支持的链接形式:
  https://y.qq.com/n/ryqq/playlist/8xxx          QQ音乐歌单
  https://c6.y.qq.com/base/fcgi-bin/u?__=xxxx    QQ音乐短链
  https://music.163.com/playlist?id=123456       网易云歌单
  https://163cn.tv/xxxx                          网易云短链
  https://open.spotify.com/playlist/37i9dQ...    Spotify 歌单
  spotify:playlist:37i9dQ...                     Spotify URI
"""
import json
import os
import re
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from difflib import SequenceMatcher  # noqa: E402

from charts import (build_index, match_one, norm, Navidrome, CONF_PATH, log,  # noqa: E402
                    art_norm, _artist_hit, agent_backfill)


def strict_match(title, artist, idx, songs, threshold=0.90):
    """歌单导入用严格匹配：必须有歌手交集，避免同名不同歌错配"""
    nt, na = norm(title), art_norm(artist)
    if not nt:
        return None
    cands = idx.get(nt, [])
    if cands:
        for c in cands:
            if _artist_hit(na, art_norm(c.get("artist"))):
                return c
        return None          # 有同名歌但歌手对不上 -> 宁可不要
    best, best_r = None, 0.0
    for so in songs:
        nti = norm(so.get("title"))
        if not nti:
            continue
        if na and not _artist_hit(na, art_norm(so.get("artist"))):
            continue
        r = SequenceMatcher(None, nt, nti).ratio()
        if r > best_r:
            best, best_r = so, r
    if best is not None and best_r >= threshold:
        return best
    return None

TOKEN_FILE = os.path.join(BASE_DIR, ".spotify_token.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
QISHUI_HDR = {"User-Agent": UA, "Referer": "https://music.douyin.com/"}


# ---------------- 链接解析 ----------------

def resolve_url(url):
    """返回 (platform, id, 原始链接)"""
    u = url.strip()
    low = u.lower()

    if low.startswith("spotify:playlist:"):
        return "spotify", u.split(":")[-1], u
    m = re.search(r"open\.spotify\.com/(?:intl-[a-z\-]+/)?playlist/([A-Za-z0-9]+)", u)
    if m:
        return "spotify", m.group(1), u

    m = re.search(r"music\.163\.com/(?:#/)?playlist\?id=(\d+)", u)
    if m:
        return "netease", m.group(1), u
    m = re.search(r"music\.163\.com/playlist/(\d+)", u)
    if m:
        return "netease", m.group(1), u

    m = re.search(r"y\.qq\.com/n/ryqq/playlist/(\d+)", u)
    if m:
        return "qq", m.group(1), u
    m = re.search(r"y\.qq\.com.*[?&]id=(\d+)", u)
    if m:
        return "qq", m.group(1), u
    m = re.search(r"i\.y\.qq\.com.*[?&]id=(\d+)", u)
    if m:
        return "qq", m.group(1), u

    m = re.search(r"music\.douyin\.com/qishui/(?:share/)?(chart|playlist)[/?]?(?:id=)?(\d{15,20})", u)
    if m:
        return "qishui", f"{m.group(1)}:{m.group(2)}", u
    m = re.search(r"(?:qishui\.douyin\.com|music\.douyin\.com/qishui)/(?:s|share)/([A-Za-z0-9]+)", u)
    if m:
        return "qishui", f"short:{m.group(1)}", u

    # 短链：跟随跳转后再判断
    if re.search(r"(163cn\.tv|c6\.y\.qq\.com/base/fcgi-bin/u)", low):
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
            final = r.url
            log(f"短链跳转: {final[:120]}")
            m = re.search(r"playlist/(\d+)|[?&]id=(\d+)", final)
            if "163" in final:
                return "netease", (m.group(1) or m.group(2)), final
            if "qq.com" in final:
                return "qq", (m.group(1) or m.group(2)), final
        except Exception as e:
            log(f"短链解析失败: {e}")

    raise ValueError(f"无法识别的链接: {u}")


# ---------------- 各平台抓取 ----------------

def fetch_netease(pid):
    """返回 (歌单名, [(title, artist)])"""
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    cookies = {"appver": "2.0.2", "os": "pc"}
    detail = requests.get(
        f"https://music.163.com/api/v6/playlist/detail?id={pid}&n=1000&s=0",
        headers=headers, cookies=cookies, timeout=20)
    d = detail.json()
    name = (d.get("playlist") or {}).get("name") or f"网易云歌单{pid}"
    tracks = (d.get("playlist") or {}).get("tracks") or []
    out = []
    for t in tracks:
        ar = ", ".join(a.get("name", "") for a in (t.get("ar") or t.get("artists") or []))
        out.append((t.get("name", ""), ar))
    # tracks 可能被截断，用全量接口补齐
    if len(out) < ((d.get("playlist") or {}).get("trackCount") or 0):
        try:
            allp = requests.get(
                f"https://music.163.com/api/playlist/track/all?id={pid}&limit=1000&offset=0",
                headers=headers, cookies=cookies, timeout=25)
            songs = allp.json().get("songs") or []
            if songs:
                out = [(s.get("name", ""),
                        ", ".join(a.get("name", "") for a in (s.get("ar") or s.get("artists") or [])))
                       for s in songs]
        except Exception as e:
            log(f"网易云全量补齐失败(用已抓到的 {len(out)} 首): {e}")
    return name, out


def fetch_qq(disstid):
    """返回 (歌单名, [(title, artist)])"""
    url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    out, name = [], f"QQ歌单{disstid}"
    begin, step = 0, 500
    while begin < 5000:
        payload = {
            "req_0": {
                "module": "music.srfDissInfo.aiDissInfo",
                "method": "uniform_get_Dissinfo",
                "param": {"disstid": int(disstid), "enc_host_uin": "", "tag": 1,
                          "userinfo": 1, "song_begin": begin, "song_num": step, "order": 1},
            }
        }
        r = requests.post(url, data=json.dumps(payload),
                          headers={"User-Agent": UA, "Referer": "https://y.qq.com/",
                                   "Content-Type": "application/json"},
                          timeout=25)
        data = (r.json().get("req_0") or {}).get("data") or {}
        info = data.get("dirinfo") or {}
        if begin == 0:
            name = info.get("title") or data.get("dissname") or name
        songs = data.get("songlist") or []
        if not songs:
            break
        for s in songs:
            singers = ", ".join(x.get("name", "") for x in (s.get("singer") or []))
            out.append((s.get("name", ""), singers))
        total = data.get("total_song_num") or 0
        begin += step
        if total and begin >= total:
            break
        if len(songs) < step:
            break
    return name, out


def spotify_token(conf):
    """client_credentials 取 token，带本地缓存"""
    auth = conf.get("spotify_auth") or {}
    cid, secret = auth.get("client_id"), auth.get("client_secret")
    if not cid or not secret:
        raise RuntimeError("config.json 缺少 spotify_auth.client_id / client_secret")
    if os.path.exists(TOKEN_FILE):
        try:
            c = json.load(open(TOKEN_FILE, encoding="utf-8"))
            if c.get("expires", 0) > time.time() + 60:
                return c["access_token"]
        except Exception:
            pass
    r = requests.post("https://accounts.spotify.com/api/token",
                      data={"grant_type": "client_credentials"},
                      auth=(cid, secret), timeout=20)
    r.raise_for_status()
    j = r.json()
    tok = j["access_token"]
    json.dump({"access_token": tok, "expires": time.time() + int(j.get("expires_in", 3600))},
              open(TOKEN_FILE, "w", encoding="utf-8"))
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def _spotify_embed(pid, proxies):
    r = requests.get(f"https://open.spotify.com/embed/playlist/{pid}",
                     headers={"User-Agent": UA}, proxies=proxies, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"embed HTTP {r.status_code}")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  r.text, re.S)
    if not m:
        raise RuntimeError("embed 页未解析到 __NEXT_DATA__")
    ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    out = [(t.get("title", ""), t.get("subtitle", "")) for t in (ent.get("trackList") or [])
           if t.get("title")]
    return ent.get("name") or f"Spotify歌单{pid}", out


def fetch_spotify(pid, conf, use_proxy=False):
    """优先走公开 embed 页（无需 Premium），失败再回退 Web API"""
    proxy_url = conf.get("proxy")
    proxies = {"http": proxy_url, "https": proxy_url} if (use_proxy and proxy_url) else None
    try:
        name, out = _spotify_embed(pid, proxies)
        log(f"[spotify] 公共接口(embed) 取到 {len(out)} 首")
        return name, out
    except Exception as e:
        log(f"[spotify] embed 失败({e})，回退 Web API")

    tok = spotify_token(conf)
    h = {"Authorization": f"Bearer {tok}"}
    meta = requests.get(f"https://api.spotify.com/v1/playlists/{pid}",
                        headers=h, params={"fields": "name,tracks.total"},
                        proxies=proxies, timeout=25).json()
    name = meta.get("name") or f"Spotify歌单{pid}"
    total = ((meta.get("tracks") or {}).get("total")) or 0
    out, offset = [], 0
    while offset < max(total, 1):
        r = requests.get(f"https://api.spotify.com/v1/playlists/{pid}/tracks",
                         headers=h,
                         params={"limit": 100, "offset": offset,
                                 "fields": "items(track(name,artists(name)))"},
                         proxies=proxies, timeout=25)
        j = r.json()
        items = j.get("items") or []
        if not items:
            break
        for it in items:
            t = it.get("track") or it.get("item") or {}
            if not t or not t.get("name"):
                continue
            out.append((t.get("name", ""),
                        ", ".join(a.get("name", "") for a in (t.get("artists") or []))))
        offset += 100
        if offset >= total:
            break
    return name, out


def _walk_tracks(o, out):
    """递归提取 {"name":..., "artists":[{name}...]} 结构"""
    if isinstance(o, dict):
        if o.get("name") and isinstance(o.get("artists"), list) and o["artists"] \
                and isinstance(o["artists"][0], dict):
            out.append((o["name"],
                        ", ".join(a.get("name", "") for a in o["artists"])))
            return
        for v in o.values():
            _walk_tracks(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk_tracks(v, out)


def fetch_qishui(spec, use_proxy=False):
    """汽水音乐榜单/歌单。spec: chart:ID / playlist:ID / short:CODE"""
    kind, _, pid = spec.partition(":")
    kwargs = {}
    if use_proxy:
        p = json.load(open(CONF_PATH, encoding="utf-8")).get("proxy")
        if p:
            kwargs["proxies"] = {"http": p, "https": p}
    if kind == "short":
        r = requests.get(f"https://qishui.douyin.com/s/{pid}",
                         headers=QISHUI_HDR, timeout=20, allow_redirects=True, **kwargs)
        final = r.url
        log(f"[qishui] 短链跳转: {final[:120]}")
        m = re.search(r"(chart|playlist)[/?]?(?:id=)?(\d{15,20})", final)
        if m:
            kind, pid = m.group(1), m.group(2)
        else:
            m = re.search(r'"(\d{15,20})"', r.text)
            if not m:
                raise RuntimeError(f"汽水短链未解析出 id: {final[:120]}")
            kind, pid = "playlist", m.group(1)

    if kind == "chart":
        url = f"https://api.qishui.com/luna/pc/charts/{pid}"
        j = requests.get(url, headers=QISHUI_HDR, timeout=30, **kwargs).json()
        name = ((j.get("chart") or {}).get("title") or f"汽水榜单{pid}")
    else:
        url = f"https://api.qishui.com/luna/pc/playlist/detail?playlist_id={pid}"
        j = requests.get(url, headers=QISHUI_HDR, timeout=30, **kwargs).json()
        name = f"汽水歌单{pid}"
        m = re.search(r'"(?:title|name)"\s*:\s*"([^"]{1,60})"', json.dumps(j, ensure_ascii=False))
        if m:
            name = m.group(1)
    out = []
    _walk_tracks(j, out)
    # 去重保序
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    if not uniq:
        raise RuntimeError(f"汽水接口未解析到曲目: {str(j)[:150]}")
    return name, uniq


# ---------------- 主流程 ----------------

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    custom_name = None
    if "--name" in sys.argv:
        i = sys.argv.index("--name")
        if i + 1 < len(sys.argv):
            custom_name = sys.argv[i + 1]
            args = [a for a in args if a != custom_name]
    use_proxy = "--proxy" in sys.argv
    loose = "--loose" in sys.argv

    urls = [a for a in args if re.match(r"^(https?://|spotify:)", a)]
    if not urls:
        print(__doc__)
        sys.exit(1)

    conf = json.load(open(CONF_PATH, encoding="utf-8"))
    nd = Navidrome(conf["navidrome"])
    songs = nd.all_songs()
    idx = build_index(songs)
    threshold = float(conf.get("fuzzy_threshold", 0.86))
    log(f"曲库载入: {len(songs)} 首")

    report = []
    all_miss = []
    for u in urls:
        try:
            plat, pid, _ = resolve_url(u)
            log(f"[{plat}] id={pid}")
            if plat == "netease":
                name, tracks = fetch_netease(pid)
            elif plat == "qq":
                name, tracks = fetch_qq(pid)
            elif plat == "qishui":
                name, tracks = fetch_qishui(pid, use_proxy)
            else:
                name, tracks = fetch_spotify(pid, conf, use_proxy)

            list_name = custom_name or name or f"{plat}歌单"
            matched, miss = [], []
            for title, artist in tracks:
                if loose:
                    song, _reason = match_one(title, artist, idx, songs, threshold)
                else:
                    song = strict_match(title, artist, idx, songs)
                sid = song.get("id") if song else None
                if sid:
                    matched.append(sid)
                else:
                    miss.append((title, artist))
            # 去重保序
            seen, uniq = set(), []
            for s in matched:
                if s not in seen:
                    seen.add(s)
                    uniq.append(s)

            log(f"[{plat}] 《{list_name}》 抓取 {len(tracks)} 首 / 匹配 {len(uniq)} 首 / 未匹配 {len(miss)} 首")

            if miss and not dry:
                import csv as _csv
                import shutil as _shutil
                from datetime import datetime as _dt
                rep = conf.get("output_dir") or os.path.join(BASE_DIR, "reports")
                os.makedirs(rep, exist_ok=True)
                safe = re.sub(r'[\\/:*?"<>|]', "_", list_name)[:60]
                cp = os.path.join(rep, f"歌单未匹配-{safe}-{_dt.now():%Y-%m-%d}.csv")
                with open(cp, "w", encoding="utf-8-sig", newline="") as fh:
                    w = _csv.writer(fh)
                    w.writerow(["平台", "歌单", "歌曲名", "歌手"])
                    for _t, _a in miss:
                        w.writerow([plat, list_name, _t, _a])
                log(f"未匹配报表: {cp}")
                mirror = "/volume1/共享音乐/_榜单报表"
                if os.path.isdir(mirror):
                    try:
                        _shutil.copy2(cp, os.path.join(mirror, os.path.basename(cp)))
                    except Exception:
                        pass
            if not dry and uniq:
                exist = {p.get("name"): p.get("id") for p in nd.playlists()}
                if list_name in exist:
                    nd.delete_playlist(exist[list_name])
                    log(f"[{plat}] 已删除同名旧列表 {list_name}")
                if uniq:
                    nd.create_playlist(list_name, uniq)
                    log(f"[{plat}] 已创建播放列表《{list_name}》{len(uniq)} 首")
            report.append({"platform": plat, "id": pid, "name": list_name,
                           "total": len(tracks), "matched": len(uniq), "miss": miss[:200]})
            all_miss.extend(miss)
        except Exception as e:
            log(f"[FAIL] {u} -> {e}")
            report.append({"platform": "?", "id": u, "error": str(e)})

    if all_miss and not dry:
        agent_backfill(conf, all_miss)

    print("\n==== SUMMARY_JSON ====")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
