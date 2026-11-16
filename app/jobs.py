from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Any

from .client import MusicApiClient, MusicApiError, RateLimitExceeded
from .library import organize_library
from .matching import fix_text, pick_best
from .navidrome import NavidromeClient


class JobError(Exception):
    """后台任务创建失败。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artist_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " / ".join(fix_text(item) for item in value if item)
    return fix_text(value)


async def ensure_track(
    client: MusicApiClient,
    track: dict[str, Any],
    *,
    threshold: float = 0.9,
    source: str = "netease",
) -> dict[str, Any]:
    """搜索并下载单首缺失歌曲，返回可直接放进任务结果的字典。"""
    title = fix_text(track.get("title"))
    artist = fix_text(track.get("artist"))
    track_source = fix_text(track.get("source")) or source

    if not title:
        return {"title": title, "artist": artist, "status": "invalid", "error": "歌名为空"}

    candidates = await client.search(source=track_source, name=title, pages=1)
    best, score = pick_best(candidates, title, artist, threshold)

    if best is None and artist:
        candidates = await client.search(
            source=track_source,
            name=f"{title} {artist}",
            pages=1,
        )
        best, score = pick_best(candidates, title, artist, threshold)

    if best is None:
        return {
            "title": title,
            "artist": artist,
            "status": "not_found",
            "score": score,
        }

    candidate_artist = _artist_text(best.get("artist"))
    payload = {
        "source": fix_text(best.get("source")) or track_source,
        "id": str(best.get("id")),
        "name": fix_text(best.get("name")) or title,
        "artist": candidate_artist or artist,
        "album": fix_text(best.get("album")),
        "pic_id": str(best.get("pic_id") or ""),
        "br": "999",
        "lyric": True,
        "vip": False,
    }

    response = await client.download(payload)
    if not isinstance(response, dict) or response.get("status") != "success":
        return {
            "title": title,
            "artist": artist,
            "status": "download_failed",
            "response": response,
        }

    return {
        "title": title,
        "artist": artist,
        "status": "downloaded",
        "filename": response.get("filename"),
        "matched_name": payload["name"],
        "matched_artist": payload["artist"],
        "score": score,
    }


class JobManager:
    """单 worker 串行执行补歌任务，天然受上游限流约束。"""

    def __init__(
        self,
        client: MusicApiClient,
        *,
        library_dir: str,
        download_dir: str | None = None,
        match_threshold: float = 0.9,
        match_source: str = "netease",
        navidrome: NavidromeClient | None = None,
        scan_wait: bool = True,
    ) -> None:
        self._client = client
        self._library_dir = library_dir
        self._download_dir = download_dir or library_dir
        self._match_threshold = match_threshold
        self._match_source = match_source
        self._navidrome = navidrome
        self._scan_wait = scan_wait

        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._worker: asyncio.Task | None = None

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    def submit(
        self,
        tracks: list[dict[str, Any]],
        *,
        organize: bool = True,
        scan: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not tracks:
            raise JobError("tracks 不能为空")

        self._prune()
        job_id = secrets.token_hex(8)
        job = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "total": len(tracks),
            "processed": 0,
            "downloaded": 0,
            "not_found": 0,
            "failed": 0,
            "current": "",
            "tracks": tracks,
            "results": [],
            "organize": organize,
            "scan": scan,
            "dry_run": dry_run,
            "created_at": _now(),
            "finished_at": None,
            "error": None,
        }
        self._jobs[job_id] = job
        self._queue.put_nowait(job_id)
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        """任务队列快照，供监控大盘轮询。"""
        counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
        for job in self._jobs.values():
            status = job.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1

        latest = sorted(
            self._jobs.values(),
            key=lambda job: job.get("created_at") or "",
            reverse=True,
        )
        items = []
        for job in latest[:limit]:
            failures = [
                {
                    "title": result.get("title"),
                    "artist": result.get("artist"),
                    "status": result.get("status"),
                    "error": result.get("error") or result.get("response"),
                }
                for result in (job.get("results") or [])
                if result.get("status") != "downloaded"
            ]
            items.append(
                {
                    "job_id": job.get("job_id"),
                    "status": job.get("status"),
                    "stage": job.get("stage"),
                    "total": job.get("total"),
                    "processed": job.get("processed"),
                    "downloaded": job.get("downloaded"),
                    "not_found": job.get("not_found"),
                    "failed": job.get("failed"),
                    "current": job.get("current"),
                    "created_at": job.get("created_at"),
                    "finished_at": job.get("finished_at"),
                    "error": job.get("error"),
                    "failures": failures[-50:],
                }
            )

        running = next((item for item in items if item["status"] == "running"), None)
        return {
            "counts": counts,
            "queue_size": self._queue.qsize(),
            "worker_running": self._worker is not None and not self._worker.done(),
            "current_job_id": running["job_id"] if running else None,
            "items": items,
        }

    def _prune(self) -> None:
        if len(self._jobs) < 50:
            return
        finished = [
            job_id for job_id, job in self._jobs.items()
            if job["status"] in {"completed", "failed"}
        ]
        for job_id in finished[: max(len(self._jobs) - 49, 0)]:
            self._jobs.pop(job_id, None)

    async def _run_worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            try:
                if job is not None:
                    await self._run_job(job)
            finally:
                self._queue.task_done()

    async def _run_job(self, job: dict[str, Any]) -> None:
        job["status"] = "running"
        job["stage"] = "searching"
        try:
            tracks = job["tracks"]
            for index, track in enumerate(tracks):
                job["current"] = f"{track.get('title', '')} - {track.get('artist', '')}"
                try:
                    result = await self._process_track(track)
                except Exception as exc:
                    result = {
                        "title": track.get("title"),
                        "artist": track.get("artist"),
                        "status": "failed",
                        "error": str(exc),
                    }

                job["results"].append(result)
                job["processed"] = index + 1
                status = result.get("status")
                if status == "downloaded":
                    job["downloaded"] += 1
                elif status == "not_found":
                    job["not_found"] += 1
                else:
                    job["failed"] += 1
                job["current"] = ""

            if job["organize"]:
                job["stage"] = "organizing"
                try:
                    job["organize_result"] = await asyncio.to_thread(
                        organize_library,
                        self._library_dir,
                        source_dir=self._download_dir,
                        dry_run=job["dry_run"],
                    )
                except Exception as exc:
                    job["organize_result"] = {"error": str(exc)}

            if job["scan"]:
                job["stage"] = "scanning"
                if self._navidrome is None:
                    job["scan_result"] = {"skipped": "未配置 Navidrome"}
                else:
                    try:
                        job["scan_result"] = await self._navidrome.start_scan(wait=self._scan_wait)
                    except Exception as exc:
                        job["scan_result"] = {"error": str(exc)}

            job["status"] = "completed"
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            job["stage"] = "done"
            job["current"] = ""
            job["finished_at"] = _now()

    async def _process_track(self, track: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                return await ensure_track(
                    self._client,
                    track,
                    threshold=self._match_threshold,
                    source=self._match_source,
                )
            except RateLimitExceeded as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(min(max(exc.retry_after, 1.0), 600.0) + 1)
            except MusicApiError:
                raise

        return {
            "title": track.get("title"),
            "artist": track.get("artist"),
            "status": "failed",
            "error": str(last_error) if last_error else "重试次数耗尽",
        }
