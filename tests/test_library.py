from pathlib import Path

from app import library


def test_build_target_sanitizes_paths(tmp_path):
    target = library.build_target(tmp_path, "A/B", "C:D", "E", ".mp3")
    assert target.parent == tmp_path / "C_D" / "E"
    assert target.name == "C_D - A_B.mp3"


def test_decode_text_bytes_gb18030():
    assert library.decode_text_bytes("歌词".encode("gb18030")) == "歌词"


def test_organize_moves_audio_and_transcodes_lyrics(tmp_path, monkeypatch):
    root = tmp_path / "music"
    incoming = root / "incoming"
    incoming.mkdir(parents=True)
    audio = incoming / "track.mp3"
    audio.write_bytes(b"not really audio")
    (incoming / "track.lrc").write_bytes("第一句歌词".encode("gb18030"))

    monkeypatch.setattr(
        library, "read_tags", lambda _path: ("小方桌", "小春", "平凡的一天", "")
    )

    result = library.organize_library(root)

    target = root / "小春" / "平凡的一天" / "小春 - 小方桌.mp3"
    assert target.exists()
    assert not audio.exists()

    lrc = target.with_suffix(".lrc")
    assert lrc.read_text(encoding="utf-8") == "第一句歌词"
    assert not (incoming / "track.lrc").exists()
    assert not incoming.exists()
    assert result["moved"] == 1
    assert result["lyrics_written"] == 1


def test_organize_fixes_mojibake_tags(tmp_path, monkeypatch):
    root = tmp_path / "music"
    root.mkdir(parents=True, exist_ok=True)
    audio = root / "track.mp3"
    audio.write_bytes(b"not really audio")
    broken = "å°æ¹æ¡"

    monkeypatch.setattr(
        library, "read_tags", lambda _path: (broken, broken, broken, "")
    )
    monkeypatch.setattr(library, "_write_tags", lambda *args: True)

    result = library.organize_library(root)
    target = root / "小方桌" / "小方桌" / "小方桌 - 小方桌.mp3"
    assert target.exists()
    assert result["tags_fixed"] == 1


def test_organize_moves_from_download_dir_to_library(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    library_root = tmp_path / "music"
    incoming = downloads / "incoming"
    incoming.mkdir(parents=True)
    library_root.mkdir()
    audio = incoming / "track.mp3"
    audio.write_bytes(b"not really audio")
    (incoming / "track.lrc").write_bytes("第一句歌词".encode("gb18030"))

    monkeypatch.setattr(
        library, "read_tags", lambda _path: ("小方桌", "小春", "平凡的一天", "")
    )

    result = library.organize_library(library_root, source_dir=downloads)

    target = library_root / "小春" / "平凡的一天" / "小春 - 小方桌.mp3"
    assert target.exists()
    assert not audio.exists()
    assert target.with_suffix(".lrc").read_text(encoding="utf-8") == "第一句歌词"
    assert result["download_dir"] == str(downloads)
    assert result["library_dir"] == str(library_root)
    assert result["moved"] == 1
    assert result["source_dir_removed"] is True
    assert not downloads.exists()


def test_organize_dry_run_does_not_move(tmp_path, monkeypatch):
    root = tmp_path / "music"
    root.mkdir(parents=True, exist_ok=True)
    audio = root / "track.mp3"
    audio.write_bytes(b"not really audio")
    monkeypatch.setattr(
        library, "read_tags", lambda _path: ("小方桌", "小春", "平凡的一天", "")
    )

    result = library.organize_library(root, dry_run=True)
    assert audio.exists()
    assert result["planned"] == 1
    assert result["moved"] == 0


def test_organize_rejects_missing_dir(tmp_path):
    import pytest

    with pytest.raises(library.LibraryError):
        library.organize_library(Path(tmp_path) / "missing")
