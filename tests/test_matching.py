from app.matching import fix_text, norm, pick_best


def test_fix_text_repairs_mojibake():
    broken = "å°æ¹æ¡"
    assert fix_text(broken) == "小方桌"


def test_norm_handles_traditional_chinese():
    assert norm("張學友") == norm("张学友")


def test_pick_best_prefers_studio_version():
    candidates = [
        {"id": "1", "name": "小方桌 (Live)", "artist": ["小春"]},
        {"id": "2", "name": "小方桌", "artist": ["小春"]},
        {"id": "3", "name": "掉漆小方桌", "artist": ["小春"]},
    ]
    best, score = pick_best(candidates, "小方桌", "小春")
    assert best is not None
    assert best["id"] == "2"
    assert score >= 0.9


def test_pick_best_rejects_wrong_artist():
    candidates = [{"id": "1", "name": "小方桌", "artist": ["别人"]}]
    best, _ = pick_best(candidates, "小方桌", "小春")
    assert best is None


def test_pick_best_allows_missing_artist():
    candidates = [{"id": "1", "name": "小方桌", "artist": []}]
    best, _ = pick_best(candidates, "小方桌", "")
    assert best is not None
