#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日音乐榜单更新
- 抓取 网易云 / QQ音乐 / Spotify / Apple Music 榜单
- 与 Navidrome 曲库做标题+歌手匹配
- 按日期重建播放列表（同名旧列表先删除）
- 输出未匹配清单，便于扩充曲库
用法:
  python3 charts.py            # 全量执行
  python3 charts.py --dry-run  # 只抓取+匹配+出报表，不写播放列表
"""
import csv
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import requests

try:
    from opencc import OpenCC
    _CC = OpenCC("t2s")
    def _t2s(s: str) -> str:
        return _CC.convert(s)
except Exception:
    try:
        from zhconv import convert as _zh_convert
        def _t2s(s: str) -> str:
            return _zh_convert(s, "zh-cn")
    except Exception:
        def _t2s(s: str) -> str:
            return s

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.getenv("CHARTS_CONFIG") or os.path.join(BASE_DIR, "config.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

NOW = datetime.now()
DATE_MAP = {
    "date": NOW.strftime("%m.%d"),        # 11.15
    "date_y": NOW.strftime("%y.%m.%d"),   # 26.11.15
    "date_full": NOW.strftime("%Y-%m-%d"),
}

LOG_LINES = []


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)


# ---------------- 抓取 ----------------

def _session(use_proxy, proxy):
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if use_proxy and proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def fetch_netease(cfg, proxy):
    pid = cfg["id"]
    limit = int(cfg.get("limit", 200))
    s = _session(False, proxy)
    hdr = {"Referer": "https://music.163.com/"}
    r = s.get("https://music.163.com/api/v6/playlist/detail",
              params={"id": pid, "n": 1000, "s": 0}, headers=hdr, timeout=25)
    pl = (r.json() or {}).get("playlist") or {}
    ids = [t.get("id") for t in pl.get("trackIds", []) if t.get("id")][:limit]
    if not ids:
        raise RuntimeError(f"网易云 playlist {pid} 未返回曲目")
    songs = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r2 = s.get("https://music.163.com/api/song/detail",
                   params={"ids": json.dumps(chunk)}, headers=hdr, timeout=25)
        songs.extend(r2.json().get("songs", []))
    order = {sid: idx for idx, sid in enumerate(ids)}
    got = {}
    for so in songs:
        got[so.get("id")] = {
            "title": so.get("name", ""),
            "artist": "/".join(a.get("name", "") for a in (so.get("ar") or so.get("artists") or []) if isinstance(a, dict) and a.get("name")),
        }
    out = []
    for sid in ids:
        if sid in got:
            out.append(got[sid])
    return out


def fetch_qq(cfg, proxy):
    """QQ音乐榜单接口单页固定 50 条，按 song_begin 翻页拼满 limit"""
    limit = int(cfg.get("limit", 100))
    out, begin, page = [], 0, 50
    while len(out) < limit and begin < 400:
        r = requests.get("https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg",
                         params={"topid": cfg["id"], "format": "json", "page": "detail",
                                 "type": "top", "song_begin": begin, "song_num": page},
                         headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
                         timeout=25)
        items = (r.json() or {}).get("songlist") or []
        if not items:
            break
        for item in items:
            d = item.get("data") or {}
            title = d.get("songname") or d.get("title") or ""
            artist = "/".join(x.get("name", "") for x in (d.get("singer") or []))
            if title:
                out.append({"title": title, "artist": artist})
        begin += page
    return out[:limit]


def fetch_qishui(cfg, proxy):
    """汽水音乐（抖音）PC 端榜单，免登录。id 为 chart_id，如热歌榜 7036274230471712007"""
    limit = int(cfg.get("limit", 100))
    s = _session(cfg.get("proxy", False), proxy)
    r = s.get(f"https://api.qishui.com/luna/pc/charts/{cfg['id']}",
              headers={"User-Agent": UA, "Referer": "https://music.douyin.com/"}, timeout=30)
    j = r.json()
    ch = j.get("chart") or {}
    if not ch:
        raise RuntimeError(f"汽水榜单返回异常: {str(j)[:120]}")
    out = []
    for it in (ch.get("track_ranks") or [])[:limit]:
        t = it.get("track") or {}
        title = t.get("name") or t.get("title") or ""
        artist = ", ".join(a.get("name", "") for a in (t.get("artists") or []))
        if title:
            out.append({"title": title, "artist": artist})
    if not out:
        raise RuntimeError(f"汽水榜单《{ch.get('title')}》未解析到曲目")
    log(f"    汽水《{ch.get('title')}》{ch.get('period')} {ch.get('count_tracks')} 首")
    return out


def fetch_spotify(cfg, proxy):
    limit = int(cfg.get("limit", 100))
    s = _session(cfg.get("proxy", True), proxy)
    r = s.get(f"https://open.spotify.com/embed/playlist/{cfg['id']}",
              headers={"User-Agent": UA}, timeout=30)
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  r.text, re.S)
    if not m:
        raise RuntimeError("Spotify embed 页未解析到 __NEXT_DATA__")
    data = json.loads(m.group(1))
    ent = data["props"]["pageProps"]["state"]["data"]["entity"]
    out = []
    for t in (ent.get("trackList") or [])[:limit]:
        title = t.get("title") or ""
        artist = t.get("subtitle") or ""
        if title:
            out.append({"title": title, "artist": artist})
    return out


def fetch_itunes(cfg, proxy):
    """Apple Music 榜单，按 storefront 地区抓取（us/tw/hk/sg/my/jp/kr ...）"""
    limit = int(cfg.get("limit", 100))
    country = cfg.get("id") or "us"
    s = _session(cfg.get("proxy", True), proxy)
    url = (f"https://rss.marketingtools.apple.com/api/v2/{country}/"
           f"music/most-played/{limit}/songs.json")
    r = s.get(url, timeout=30)
    feed = (r.json() or {}).get("feed") or {}
    out = []
    for e in (feed.get("results") or [])[:limit]:
        title = (e.get("name") or "").strip()
        artist = (e.get("artistName") or "").strip()
        if title:
            out.append({"title": title, "artist": artist})
    if not out:
        raise RuntimeError(f"Apple Music 榜单为空（地区 {country}）")
    return out


def fetch_youtube(cfg, proxy):
    """YouTube 音乐榜（kworb 汇总页，需走代理），标题按 '歌手 - 歌名' 解析"""
    limit = int(cfg.get("limit", 50))
    url = cfg.get("url") or "https://kworb.net/youtube/"
    s = _session(True, proxy)
    r = s.get(url, headers={"User-Agent": UA}, timeout=30)
    if not r.text or len(r.text) < 500:
        raise RuntimeError("kworb 页面为空（代理可能未生效）")
    titles = re.findall(r'href="video/[^"]+\.html">([^<]+)</a>', r.text)
    out = []
    for raw in titles:
        t = html.unescape(raw).strip()
        t = re.sub(r'[\s]*[\[][^\]]*(official|mv|video|lyric|audio)[^\]]*[\]]',
                   '', t, flags=re.I).strip()
        artist, title = "", t
        m = re.match(r'^(.{2,60}?)\s+[-\u2013\u2014]\s+(.+)$', t)
        if m:
            artist, title = m.group(1).strip(), m.group(2).strip()
        if title:
            out.append({"title": title, "artist": artist})
        if len(out) >= limit:
            break
    if not out:
        raise RuntimeError("kworb 未解析到榜单条目")
    return out


FETCHERS = {
    "netease": fetch_netease,
    "qq": fetch_qq,
    "qishui": fetch_qishui,
    "spotify": fetch_spotify,
    "itunes": fetch_itunes,
    "youtube": fetch_youtube,
}


# ---------------- 归一化与匹配 ----------------

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.U)
_BRACKET = re.compile(r"[\(\（\[【].*?[\)\）\]】]", re.S)
_NOISE = re.compile(
    r"\b(feat|ft|featuring|remaster(ed)?|remix|version|edit|live|acoustic|"
    r"official|mv|video|radio|demo)\b\.?", re.I)


def norm(s):
    if not s:
        return ""
    s = str(s).lower()
    s = _BRACKET.sub(" ", s)
    s = _NOISE.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    s = _t2s(s)
    return re.sub(r"\s+", "", s)


_ART_SPLIT = re.compile(r"[,/&、•;+]|feat\.?|ft\.?", re.I)

ARTIST_ALIAS = {
    # 苏打绿 / 五月天 / 常见华语歌手英文艺名（Spotify 用拼音或英文名，本地曲库是中文）
    "sodagreen": "苏打绿", "mayday": "五月天", "wuyuetian": "五月天",
    "ameichang": "张惠妹", "amei": "张惠妹", "jolin": "蔡依林", "jolintsai": "蔡依林",
    "ronghaoli": "李荣浩", "leehomwang": "王力宏", "ellachen": "陈嘉桦",
    "angelachang": "张韶涵", "yisayu": "郁可唯", "kimberleychen": "陈芳语",
    "kenjiwu": "吴克群", "christinefan": "范玮琪", "rainieyang": "杨丞琳",
    "askayang": "杨宗纬", "harlemyu": "庾澄庆", "jordanchan": "陈小春",
    "sandylam": "林忆莲", "jesslee": "李佳薇", "claireguo": "郭静",
    "nanquanmama": "南拳妈妈", "tank吕建忠": "吕建忠", "fanyichen": "范逸臣",
    "elvahsiao": "萧亚轩", "petstseng": "曾沛慈", "huxia": "胡夏",
    "zhangzhenyue": "张震岳", "andrewtan": "陈势安", "peiyuhung": "洪佩瑜",
    "fir": "飞儿乐团", "f.i.r.": "飞儿乐团", "queenwei": "魏如昀",
    "gem": "邓紫棋", "g.e.m.": "邓紫棋", "easonchan": "陈奕迅", "jaychou": "周杰伦",
    "jjlin": "林俊杰", "stefaniesun": "孙燕姿", "sunyanzi": "孙燕姿",
    "cheer": "陈绮贞", "chendier": "陈绮贞", "deserts": "张悬", "anpu": "张悬",
    "shaneward": "张悬", "leehom": "王力宏", "wanglihong": "王力宏",
    "jams": "萧敬腾", "jamhsiao": "萧敬腾", "alvin": "刘若英",
    "yogalin": "林宥嘉", "linyujia": "林宥嘉", "hinscheung": "张敬轩",
    "jonathanlee": "李宗盛", "peterpan": "潘裕文", "bestards": "告五人",
    "accusefive": "告五人", "yangnairwen": "杨乃文", "fayeperry": "Faye", "evanyo": "蔡旻佑", "tsaiminyu": "蔡旻佑",
}


def art_norm(s):
    """歌手归一：拆分子歌手 -> 归一化 -> 英文艺名映射为中文"""
    if not s:
        return ""
    out = []
    for atom in _ART_SPLIT.split(str(s)):
        a = norm(atom)
        if not a:
            continue
        out.append(ARTIST_ALIAS.get(a, a))
    return "".join(out)


def build_index(songs):
    idx = defaultdict(list)
    for so in songs:
        key = norm(so.get("title"))
        if key:
            idx[key].append(so)
    return idx


def _artist_hit(want_a, got_a):
    if not want_a or not got_a:
        return True
    return want_a in got_a or got_a in want_a


def match_one(title, artist, idx, songs, threshold):
    nt, na = norm(title), art_norm(artist)
    if not nt:
        return None, "歌名归一化后为空"
    cands = idx.get(nt, [])
    if cands:
        for c in cands:
            if _artist_hit(na, art_norm(c.get("artist"))):
                return c, "strong:标题精确+歌手匹配"
        if len(cands) == 1:
            return cands[0], "strong:标题精确唯一"
        return cands[0], "weak:标题精确多版本取首个"
    # 模糊
    best, best_r = None, 0.0
    for so in songs:
        ntitle = norm(so.get("title"))
        if not ntitle:
            continue
        if na and not _artist_hit(na, art_norm(so.get("artist"))):
            continue
        r = SequenceMatcher(None, nt, ntitle).ratio()
        if r > best_r:
            best, best_r = so, r
    if best is not None and best_r >= threshold:
        return best, f"weak:模糊{best_r:.2f}"
    return None, "曲库无匹配"


# ---------------- Navidrome ----------------

def _wait_agent_job(base, job_id, cfg):
    interval = float(cfg.get("poll_interval", 30))
    deadline = time.time() + float(cfg.get("job_timeout", 7200))
    last_line = ""
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/api/jobs/{job_id}", timeout=15)
            r.raise_for_status()
            job = r.json()
        except Exception as e:
            log(f"[agent] 查询任务失败: {e}")
        else:
            line = (f"{job.get('status')} {job.get('processed')}/{job.get('total')} "
                    f"下载 {job.get('downloaded')} 失败 {job.get('failed')}")
            if line != last_line:
                log(f"[agent] {line}")
                last_line = line
            if job.get("status") in ("completed", "failed"):
                return job
        time.sleep(interval)
    log("[agent] 等待补歌任务超时，任务会继续在后台执行")
    return None


def agent_backfill(conf, tracks):
    """把缺失歌曲提交给 agent API：自动搜索 -> 下载 -> 清洗 -> Navidrome 扫描。"""
    cfg = conf.get("agent_api") or {}
    if not cfg.get("enable", False):
        return []

    base = (cfg.get("base") or "http://127.0.0.1:8080").rstrip("/")
    raw_count = len(tracks)
    unique, seen = [], set()
    for title, artist in tracks:
        title = (title or "").strip()
        artist = (artist or "").strip()
        key = (norm(title), art_norm(artist))
        if not title or key in seen:
            continue
        seen.add(key)
        unique.append({"title": title, "artist": artist})

    if not unique:
        return []

    limit = int(cfg.get("max_tracks", 1000))
    if len(unique) > limit:
        log(f"[agent] 缺失 {len(unique)} 首，超过上限 {limit}，本次只提交前 {limit} 首")
        unique = unique[:limit]
    else:
        log(f"[agent] 缺失去重: {raw_count} -> {len(unique)} 首")

    jobs = []
    for index in range(0, len(unique), 500):
        chunk = unique[index:index + 500]
        payload = {
            "tracks": chunk,
            "organize": bool(cfg.get("organize", True)),
            "scan": bool(cfg.get("scan", True)),
        }
        try:
            response = requests.post(f"{base}/api/library/backfill", json=payload, timeout=30)
            if response.status_code == 429:
                log(f"[agent] 触发限流，跳过本次补歌: {response.text[:120]}")
                break
            response.raise_for_status()
            job = response.json()
            job_id = job.get("job_id")
            jobs.append(job_id)
            log(f"[agent] 已提交补歌任务 {job_id}，共 {len(chunk)} 首")
        except Exception as e:
            log(f"[agent] 提交补歌任务失败: {e}")
            break

    if cfg.get("wait", False):
        for job_id in jobs:
            _wait_agent_job(base, job_id, cfg)
    return jobs


class Navidrome:
    def __init__(self, cfg):
        self.base = cfg["base"].rstrip("/")
        self.auth = {
            "u": cfg["user"], "p": cfg["password"],
            "v": cfg.get("api_version", "1.16.1"),
            "c": cfg.get("client", "marvis"), "f": "json",
        }
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})

    def _get(self, method, pairs=None):
        params = list(self.auth.items()) + list(pairs or [])
        r = self.s.get(f"{self.base}/rest/{method}.view", params=params, timeout=40)
        data = r.json().get("subsonic-response", {})
        if data.get("status") != "ok":
            raise RuntimeError(f"{method} 失败: {data.get('error')}")
        return data

    def all_songs(self):
        data = self._get("search3", [("query", ""), ("songCount", 5000),
                                     ("artistCount", 0), ("albumCount", 0)])
        return data.get("searchResult3", {}).get("song", []) or []

    def playlists(self):
        data = self._get("getPlaylists")
        return data.get("playlists", {}).get("playlist", []) or []

    def delete_playlist(self, pid):
        self._get("deletePlaylist", [("id", pid)])

    def create_playlist(self, name, song_ids):
        pairs = [("name", name)] + [("songId", sid) for sid in song_ids]
        return self._get("createPlaylist", pairs)


def cleanup_old_chart_playlists(nd, keep_days=1):
    """只保留最近 keep_days 天的榜单列表，避免列表区被历史日期铺满"""
    pat = re.compile(r"^榜单\d{2}·.+?\s+(\d{4}-\d{2}-\d{2})$")
    cutoff = (NOW - timedelta(days=max(keep_days - 1, 0))).date()
    removed = []
    for p in nd.playlists():
        m = pat.match(p.get("name", "") or "")
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except Exception:
            continue
        if d < cutoff:
            try:
                nd.delete_playlist(p["id"])
                removed.append(p["name"])
            except Exception as e:
                log(f"清理历史列表失败 {p['name']}: {e}")
    for n in removed:
        log(f"已清理历史榜单列表 {n}")
    return len(removed)


# ---------------- 主流程 ----------------

def main():
    dry = "--dry-run" in sys.argv
    conf = json.load(open(CONF_PATH, encoding="utf-8"))
    proxy = conf.get("proxy")
    threshold = float(conf.get("fuzzy_threshold", 0.86))

    log(f"==== 榜单更新开始 {NOW.strftime('%Y-%m-%d %H:%M:%S')} ====")
    nd = Navidrome(conf["navidrome"])
    songs = nd.all_songs()
    log(f"曲库载入: {len(songs)} 首")
    idx = build_index(songs)

    existing = {}
    try:
        for p in nd.playlists():
            existing[p.get("name")] = p.get("id")
    except Exception as e:
        log(f"读取现有播放列表失败(不影响建列表): {e}")

    report_rows = []
    summary = []

    for cfg in conf.get("platforms", []):
        if not cfg.get("enabled", True):
            continue
        key = cfg["key"]
        name = cfg["name"].format(**DATE_MAP)
        try:
            t0 = time.time()
            tracks = FETCHERS[cfg["type"]](cfg, proxy)
            log(f"[{key}] 抓取 {len(tracks)} 首 ({time.time() - t0:.1f}s)")
        except Exception as e:
            log(f"[{key}] 抓取失败: {e}")
            summary.append({"key": key, "name": name, "total": 0, "matched": 0,
                            "unmatched": 0, "error": str(e)})
            continue

        matched_ids, seen = [], set()
        for i, tr in enumerate(tracks):
            song, reason = match_one(tr["title"], tr["artist"], idx, songs, threshold)
            if song is not None:
                sid = song.get("id")
                if sid and sid not in seen:
                    seen.add(sid)
                    matched_ids.append(sid)
            else:
                report_rows.append({
                    "平台": key, "榜单": name, "排名": i + 1,
                    "歌曲名": tr["title"], "歌手": tr["artist"],
                    "原因": reason,
                })
        uniq = len(set(norm(t["title"]) for t in tracks if norm(t["title"])))
        log(f"[{key}] 匹配 {len(matched_ids)} 首 / 去重后 {uniq} 首 / 未匹配 {len(report_rows)} 条(含重复)")

        if not dry and matched_ids:
            try:
                if name in existing:
                    nd.delete_playlist(existing[name])
                    log(f"[{key}] 已删除同名旧列表 {name}")
                nd.create_playlist(name, matched_ids)
                log(f"[{key}] 已创建播放列表《{name}》{len(matched_ids)} 首")
            except Exception as e:
                log(f"[{key}] 写播放列表失败: {e}")

        summary.append({"key": key, "name": name, "total": uniq,
                        "matched": len(matched_ids),
                        "unmatched": max(uniq - len(matched_ids), 0)})

    if not dry:
        cleanup_old_chart_playlists(nd, int(conf.get("keep_days", 1)))
        if report_rows:
            agent_backfill(
                conf,
                [(row["歌曲名"], row["歌手"]) for row in report_rows],
            )

    # 报表
    rep_dir = os.getenv("CHARTS_REPORTS_DIR") or os.path.join(BASE_DIR, "reports")
    os.makedirs(rep_dir, exist_ok=True)
    stamp = NOW.strftime("%Y-%m-%d")
    csv_path = os.path.join(rep_dir, f"未匹配清单-{stamp}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["平台", "榜单", "排名", "歌曲名", "歌手", "原因"])
        w.writeheader()
        for row in report_rows:
            w.writerow(row)

    md_path = os.path.join(rep_dir, f"榜单更新-{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 榜单更新报告 {stamp}\n\n")
        f.write("| 榜单 | 榜单曲目 | 已匹配 | 未匹配 |\n|---|---|---|---|\n")
        for s in summary:
            err = f"抓取失败: {s['error']}" if s.get("error") else ""
            f.write(f"| {s['name']} | {s['total']} | {s['matched']} | "
                    f"{'—' if err else s['unmatched']} |\n")
            if err:
                f.write(f"| ↳ {err} | | | |\n")
        f.write(f"\n未匹配明细: `{csv_path}`（共 {len(report_rows)} 条）\n")
        if report_rows:
            f.write("\n## 未匹配 Top 30\n\n")
            for r in report_rows[:30]:
                f.write(f"- [{r['平台']}] {r['歌曲名']} — {r['歌手']}\n")

    out_dir = conf.get("output_dir")
    if out_dir and os.path.isdir(out_dir):
        import shutil
        for p in (csv_path, md_path):
            dst = os.path.join(out_dir, os.path.basename(p))
            if os.path.abspath(dst) == os.path.abspath(p):
                continue
            try:
                shutil.copy2(p, dst)
            except Exception as e:
                log(f"复制到 output 失败: {e}")

    log_path = os.path.join(
        os.getenv("CHARTS_LOG_DIR") or os.path.join(BASE_DIR, "logs"),
        f"{stamp}.log",
    )
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES) + "\n")

    log(f"报表: {csv_path}")
    log(f"报表: {md_path}")
    log("==== 完成 ====")

    print("\n=== SUMMARY_JSON ===")
    print(json.dumps({"date": stamp, "dry_run": dry, "summary": summary,
                      "unmatched": len(report_rows),
                      "csv": csv_path, "md": md_path}, ensure_ascii=False))


if __name__ == "__main__":
    main()
