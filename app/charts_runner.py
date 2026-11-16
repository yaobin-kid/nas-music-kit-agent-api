from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class ChartsError(Exception):
    """榜单接口的基础异常。"""


class ChartsBusy(ChartsError):
    """已有榜单任务在执行。"""


class ChartsNotConfigured(ChartsError):
    """榜单代码或配置缺失。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ChartsRunner:
    """在后台线程里执行 music-charts/charts.py，并保存最近一次运行状态。"""

    def __init__(
        self,
        charts_dir: str,
        config_path: str,
        reports_dir: str,
        state_file: str,
        timeout: float = 3600.0,
        python: str | None = None,
    ) -> None:
        self.charts_dir = Path(charts_dir)
        self.config_path = Path(config_path)
        self.reports_dir = Path(reports_dir)
        self.state_file = Path(state_file)
        self.timeout = timeout
        self.python = python or sys.executable
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = self._load_state()

    # ---------- 状态 ----------

    def _load_state(self) -> dict[str, Any]:
        default: dict[str, Any] = {
            "status": "idle",
            "run_id": None,
            "dry_run": None,
            "started_at": None,
            "finished_at": None,
            "duration_seconds": None,
            "returncode": None,
            "error": None,
            "job_ids": [],
            "report_file": None,
            "log_file": None,
            "log_tail": "",
        }
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                default.update(data)
        except Exception:
            pass
        if default.get("status") == "running":
            # 进程重启后，上次运行已经中断，避免状态永远卡在 running
            default["status"] = "failed"
            default["error"] = "进程重启，任务中断"
            default["finished_at"] = _now()
        return default

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["configured"] = (
            (self.charts_dir / "charts.py").is_file()
            and self.config_path.is_file()
        )
        state["charts_dir"] = str(self.charts_dir)
        state["config_path"] = str(self.config_path)
        state["reports_dir"] = str(self.reports_dir)
        return state

    # ---------- 执行 ----------

    def refresh(self, dry_run: bool = False, wait: bool = False) -> dict[str, Any]:
        if not (self.charts_dir / "charts.py").is_file():
            raise ChartsNotConfigured(
                f"找不到 {self.charts_dir / 'charts.py'}；请确认 CHARTS_HOST_DIR 挂载目录内包含 charts.py"
            )
        if not self.config_path.is_file():
            raise ChartsNotConfigured(
                f"找不到榜单配置 {self.config_path}；请把 config.example.json 复制为 config.json 放进 CHARTS_HOST_DIR 挂载目录"
            )

        with self._lock:
            if self._state.get("status") == "running":
                raise ChartsBusy("已有榜单任务在执行，请稍后再试")
            run_id = uuid.uuid4().hex[:12]
            self._state = {
                "status": "running",
                "run_id": run_id,
                "dry_run": dry_run,
                "started_at": _now(),
                "finished_at": None,
                "duration_seconds": None,
                "returncode": None,
                "error": None,
                "job_ids": [],
                "report_file": None,
                "log_file": None,
                "log_tail": "",
            }
            self._save_state()

        if wait:
            self._execute(run_id, dry_run)
        else:
            self._thread = threading.Thread(
                target=self._execute,
                args=(run_id, dry_run),
                daemon=True,
            )
            self._thread.start()
        return self.get_state()

    def _execute(self, run_id: str, dry_run: bool) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.reports_dir / f"charts-{run_id}.log"
        cmd = [self.python, "charts.py"]
        if dry_run:
            cmd.append("--dry-run")
        env = os.environ.copy()
        env["CHARTS_CONFIG"] = str(self.config_path)
        env["CHARTS_REPORTS_DIR"] = str(self.reports_dir)
        env["CHARTS_LOG_DIR"] = str(self.reports_dir / "logs")
        env["PYTHONUNBUFFERED"] = "1"

        started = datetime.now()
        output = ""
        returncode: int | None = None
        error: str | None = None
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.charts_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            returncode = proc.returncode
            if returncode != 0:
                error = f"charts.py 退出码 {returncode}"
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, str) else ""
            error = f"执行超时（{self.timeout} 秒）"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

        try:
            log_file.write_text(output, encoding="utf-8")
        except Exception:
            log_file = None

        job_ids = re.findall(r"\[agent\].*?([0-9a-f]{8,})", output)
        report_file = self._latest_report()
        finished = datetime.now()
        duration = round((finished - started).total_seconds(), 1)

        with self._lock:
            self._state.update(
                status="completed" if returncode == 0 else "failed",
                finished_at=finished.astimezone().isoformat(timespec="seconds"),
                duration_seconds=duration,
                returncode=returncode,
                error=error,
                job_ids=job_ids,
                report_file=str(report_file) if report_file else None,
                log_file=str(log_file) if log_file else None,
                log_tail=output[-8000:],
            )
            self._save_state()

    def _latest_report(self) -> Path | None:
        try:
            reports = sorted(
                self.reports_dir.glob("未匹配清单-*.csv"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return None
        return reports[0] if reports else None

    def latest_report(self) -> Path | None:
        state = self.get_state()
        path = state.get("report_file")
        if path and Path(path).is_file():
            return Path(path)
        return self._latest_report()
