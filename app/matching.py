from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import ftfy

try:
    from zhconv import convert as _zh_convert
except Exception:  # pragma: no cover - 依赖缺失时退化为原文
    def _zh_convert(text: str, _locale: str = "zh-cn") -> str:
        return text


_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.U)
_BRACKET = re.compile(r"[\(\（\[【].*?[\)\）\]】]", re.S)
_NOISE = re.compile(
    r"\b(feat|ft|featuring|remaster(ed)?|remix|version|edit|live|acoustic|"
    r"official|mv|video|radio|demo)\b\.?",
    re.I,
)
_ART_SPLIT = re.compile(r"[,/&、•;+]|feat\.?|ft\.?", re.I)
_VERSION_WORDS = ("live", "remix", "instrumental", "karaoke", "dj", "cover", "伴奏", "纯音乐", "翻唱")


ARTIST_ALIAS = {
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
    "accusefive": "告五人", "yangnairwen": "杨乃文", "fayeperry": "Faye",
    "evanyo": "蔡旻佑", "tsaiminyu": "蔡旻佑",
}


def fix_text(value: Any) -> str:
    """修复常见的标签乱码，并做 NFC 归一化。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = str(value).replace("\x00", "")
    text = ftfy.fix_text(text)
    return unicodedata.normalize("NFC", text).strip()


def _t2s(text: str) -> str:
    try:
        return _zh_convert(text, "zh-cn")
    except Exception:
        return text


def norm(value: Any) -> str:
    if not value:
        return ""
    text = fix_text(value).lower()
    text = _BRACKET.sub(" ", text)
    text = _NOISE.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    text = _t2s(text)
    return re.sub(r"\s+", "", text)


def _join_artists(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " / ".join(fix_text(v) for v in value if v)
    return fix_text(value)


def art_norm(value: Any) -> str:
    """歌手归一：拆分子歌手 -> 归一化 -> 英文艺名映射为中文。"""
    if not value:
        return ""
    atoms = []
    for atom in _ART_SPLIT.split(_join_artists(value)):
        normalized = norm(atom)
        if normalized:
            atoms.append(ARTIST_ALIAS.get(normalized, normalized))
    return "".join(atoms)


def _artist_hit(want: str, got: str) -> bool:
    if not want or not got:
        return True
    return want in got or got in want


def _version_penalty(query_title: str, candidate_title: str) -> float:
    query = fix_text(query_title).lower()
    candidate = fix_text(candidate_title).lower()
    penalty = 0.0
    for word in _VERSION_WORDS:
        if word in candidate and word not in query:
            penalty += 0.2
    return min(penalty, 0.6)


def pick_best(
    candidates: Any,
    title: str,
    artist: str = "",
    threshold: float = 0.9,
) -> tuple[dict[str, Any] | None, float]:
    """从上游搜索结果里挑选最可信的一首。

    要求标题相似度达到阈值，并且歌手有交集；带 Live/Remix/伴奏等
    版本词但查询里没有的候选会被扣分。
    """
    if not isinstance(candidates, list) or not candidates:
        return None, 0.0

    want_title = norm(title)
    want_artist = art_norm(artist)
    if not want_title:
        return None, 0.0

    best: dict[str, Any] | None = None
    best_score = 0.0

    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("id"):
            continue

        candidate_title = norm(candidate.get("name"))
        if not candidate_title:
            continue

        title_ratio = 1.0 if want_title == candidate_title else SequenceMatcher(
            None, want_title, candidate_title
        ).ratio()
        if title_ratio < threshold:
            continue

        candidate_artist = art_norm(candidate.get("artist"))
        if want_artist and candidate_artist and not _artist_hit(want_artist, candidate_artist):
            continue

        score = title_ratio
        if want_artist and candidate_artist:
            score += 0.05
        score -= _version_penalty(title, candidate.get("name"))

        if score > best_score:
            best = candidate
            best_score = score

    if best is not None and best_score >= threshold:
        return best, round(best_score, 3)
    return None, round(best_score, 3)
