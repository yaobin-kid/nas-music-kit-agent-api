from __future__ import annotations

import json

import pytest

from app.charts_runner import ChartsNotConfigured, ChartsRunner


def _runner(tmp_path):
    charts_dir = tmp_path / "charts"
    charts_dir.mkdir()
    (charts_dir / "charts.py").write_text(
        "import os\n"
        "print('[agent] 已提交补歌任务 abcdef1234567890，共 2 首')\n"
        "print(os.getenv('CHARTS_CONFIG'))\n"
        "print(os.getenv('CHARTS_REPORTS_DIR'))\n",
        encoding="utf-8",
    )
    config = charts_dir / "config.json"
    config.write_text("{}", encoding="utf-8")
    return ChartsRunner(
        charts_dir=str(charts_dir),
        config_path=str(config),
        reports_dir=str(tmp_path / "reports"),
        state_file=str(tmp_path / "state.json"),
        timeout=30,
    )


def test_charts_runner_runs_and_parses_job_id(tmp_path):
    runner = _runner(tmp_path)
    state = runner.refresh(dry_run=True, wait=True)
    assert state["status"] == "completed"
    assert state["returncode"] == 0
    assert state["job_ids"] == ["abcdef1234567890"]
    assert state["log_file"]
    assert "abcdef1234567890" in state["log_tail"]

    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["job_ids"] == ["abcdef1234567890"]


def test_charts_runner_requires_config(tmp_path):
    charts_dir = tmp_path / "charts"
    charts_dir.mkdir()
    (charts_dir / "charts.py").write_text("print('ok')", encoding="utf-8")
    runner = ChartsRunner(
        charts_dir=str(charts_dir),
        config_path=str(tmp_path / "missing.json"),
        reports_dir=str(tmp_path / "reports"),
        state_file=str(tmp_path / "state.json"),
    )
    with pytest.raises(ChartsNotConfigured):
        runner.refresh(wait=True)
