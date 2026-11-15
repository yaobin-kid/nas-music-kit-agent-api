from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes
from mutagen import File

from .matching import fix_text

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".m4b", ".aac", ".ogg", ".opus", ".wav", ".ape", ".wma"}
SKIP_DIRS = {"@eaDir", "#recycle", "__pycache__", ".git", ".svn", "lost+found"}
INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


class LibraryError(Exception):
    """媒体库清洗失败。"""


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def _read_embedded_lyrics(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.id3 import ID3

            tags = ID3(str(path))
            frame = tags.get("USLT")
            if frame is not None:
                return str(frame.text)
        elif suffix == ".flac":
            from mutagen.flac import FLAC

            tags = FLAC(str(path)).tags or {}
            for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS"):
                if key in tags:
                    return _first(tags.get(key))
        elif suffix in {".m4a", ".mp4", ".m4b", ".aac"}:
            from mutagen.mp4 import MP4

            tags = MP4(str(path)).tags or {}
            return _first(tags.get("\xa9lyr"))
        elif suffix in {".ogg", ".opus"}:
            audio = File(str(path), easy=False)
            tags = getattr(audio, "tags", None) or {}
            for key in ("lyrics", "LYRICS"):
                if key in tags:
                    return _first(tags.get(key))
    except Exception:
        return ""
    return ""


def read_tags(path: Path) -> tuple[str, str, str, str]:
    """读取原始标签（不做乱码修复）。"""
    title = artist = album = lyrics = ""
    try:
        audio = File(str(path), easy=True)
        tags = getattr(audio, "tags", None) or {}
        title = _first(tags.get("title"))
        artist = _first(tags.get("artist"))
        album = _first(tags.get("album"))
        lyrics = _first(tags.get("lyrics"))
    except Exception:
        pass

    if not lyrics:
        lyrics = _read_embedded_lyrics(path)
    return title, artist, album, lyrics


def _split_stem(stem: str) -> tuple[str, str]:
    stem = re.sub(r"^\d+[\s._-]+", "", stem).strip()
    parts = re.split(r"\s+[-\u2013\u2014]\s+", stem, maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip(), parts[0].strip()
    return stem, ""


def _safe_component(value: str, fallback: str) -> str:
    text = fix_text(value)
    text = INVALID_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or fallback


def build_target(root: Path, title: str, artist: str, album: str, suffix: str) -> Path:
    artist_name = _safe_component(artist, "未知歌手")
    album_name = _safe_component(album, "未知专辑")
    title_name = _safe_component(title, "未知歌曲")
    filename = f"{artist_name} - {title_name}{suffix.lower()}"
    return root / artist_name / album_name / filename


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 999):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise LibraryError(f"无法生成唯一文件名: {path}")


def _cjk_score(text: str) -> float:
    total = sum(1 for char in text if not char.isspace())
    if total == 0:
        return 0.0
    cjk = sum(
        1 for char in text
        if "\u4e00" <= char <= "\u9fff" or "\u3400" <= char <= "\u4dbf"
    )
    replacement = text.count("\ufffd")
    return cjk / total - replacement / total


def decode_text_bytes(data: bytes) -> str:
    """优先 UTF-8，其次 GB18030 / Big5，最后用自动检测兜底。"""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    candidates: list[tuple[float, int, str]] = []
    for order, encoding in enumerate(("gb18030", "big5")):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        candidates.append((_cjk_score(text), -order, text))
    if candidates:
        return max(candidates)[2]

    match = from_bytes(data).best()
    if match is not None:
        try:
            return str(match)
        except Exception:
            pass

    for encoding in ("shift_jis", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    return data.decode("utf-8", errors="replace")


def _find_lrc(audio_path: Path) -> Path | None:
    try:
        for entry in audio_path.parent.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".lrc" and entry.stem.lower() == audio_path.stem.lower():
                return entry
    except OSError:
        return None
    return None


def _write_tags(path: Path, title: str, artist: str, album: str) -> bool:
    try:
        audio = File(str(path), easy=True)
        if audio is None:
            return False
        if getattr(audio, "tags", None) is None:
            audio.add_tags()
        audio["title"] = title
        audio["artist"] = artist
        audio["album"] = album
        audio.save()
        return True
    except Exception:
        return False


def _write_lrc(target: Path, text: str) -> bool:
    if target.exists():
        return False
    text = unicodedata.normalize("NFC", fix_text(text))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return True


def organize_library(
    library_dir: str | Path,
    *,
    source_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """按 Artist/Album/Artist - Title 归类媒体库。

    默认原地整理；传入 source_dir 时，从下载目录移动到媒体库目录。
    - 修复标签乱码后再写入文件
    - 同名 .lrc 一并移动并转成 UTF-8
    - 没有 sidecar 时把内嵌歌词导出为 UTF-8 .lrc
    """
    root = Path(library_dir)
    source = Path(source_dir) if source_dir else root
    if not root.is_dir():
        raise LibraryError(f"媒体库目录不存在: {root}")
    if not source.is_dir():
        raise LibraryError(f"下载目录不存在: {source}")

    scanned = moved = planned = tags_fixed = lyrics_written = skipped = 0
    errors: list[dict[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [name for name in dirnames if not name.startswith(".") and name not in SKIP_DIRS]
        if source.resolve() != root.resolve():
            current = Path(dirpath).resolve()
            if current == root.resolve():
                dirnames[:] = []
                continue

        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in AUDIO_EXTS:
                continue

            scanned += 1
            try:
                raw_title, raw_artist, raw_album, raw_lyrics = read_tags(path)
                title = fix_text(raw_title)
                artist = fix_text(raw_artist)
                album = fix_text(raw_album) or "未知专辑"

                if not title or not artist:
                    stem_title, stem_artist = _split_stem(path.stem)
                    title = title or stem_title
                    artist = artist or stem_artist

                target = build_target(root, title, artist, album, path.suffix)
                tag_changed = any(
                    fix_text(raw) != (raw or "").strip()
                    for raw in (raw_title, raw_artist, raw_album)
                ) or not raw_title or not raw_artist

                lrc_source = _find_lrc(path)
                lyrics_text = ""
                if lrc_source is not None:
                    lyrics_text = decode_text_bytes(lrc_source.read_bytes())
                elif raw_lyrics:
                    lyrics_text = raw_lyrics

                if dry_run:
                    if path.resolve() != target.resolve():
                        planned += 1
                    continue

                if path.resolve() != target.resolve():
                    target = _unique_path(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(target))
                    moved += 1
                else:
                    skipped += 1

                if tag_changed and _write_tags(target, title, artist, album):
                    tags_fixed += 1

                if lyrics_text.strip():
                    target_lrc = target.with_suffix(".lrc")
                    written = _write_lrc(target_lrc, lyrics_text)
                    if written:
                        lyrics_written += 1

                # 目标歌词已存在（无论是新写入还是原本就有），删除源目录里的 .lrc
                if (
                    lrc_source is not None
                    and lrc_source != target.with_suffix(".lrc")
                    and target.with_suffix(".lrc").exists()
                    and lrc_source.exists()
                ):
                    try:
                        lrc_source.unlink()
                    except OSError:
                        pass
            except Exception as exc:
                if len(errors) < 100:
                    errors.append({"file": str(path), "error": str(exc)})

    # 清理源目录里的空子目录，保留源根目录
    source_dirs_removed = 0
    for dirpath, _dirnames, _filenames in os.walk(source, topdown=False):
        current = Path(dirpath)
        if current.resolve() == source.resolve():
            continue
        try:
            if not any(current.iterdir()):
                current.rmdir()
                source_dirs_removed += 1
        except OSError:
            pass

    # 源根目录为空时删除；/music 是 nas-music-kit 的固定下载目录，必须保留
    source_dir_removed = False
    if source.resolve() != root.resolve() and source != Path("/music"):
        try:
            if not any(source.iterdir()) and not os.path.ismount(source):
                source.rmdir()
                source_dir_removed = True
        except OSError:
            pass

    return {
        "library_dir": str(root),
        "download_dir": str(source),
        "dry_run": dry_run,
        "scanned": scanned,
        "moved": moved,
        "planned": planned,
        "tags_fixed": tags_fixed,
        "lyrics_written": lyrics_written,
        "skipped": skipped,
        "source_dirs_removed": source_dirs_removed,
        "source_dir_removed": source_dir_removed,
        "errors": errors,
    }
