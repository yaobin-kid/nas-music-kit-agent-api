from __future__ import annotations

import asyncio
import math
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .client import MusicApiClient, MusicApiError, RateLimitExceeded, RateLimiter
from .jobs import JobError, JobManager
from .library import LibraryError, organize_library
from .navidrome import NavidromeClient, NavidromeError, NavidromeSync
from .playlist_import import import_playlists

APP_VERSION = os.getenv("APP_VERSION", __version__)


class DownloadRequest(BaseModel):
    """对应上游 /api/download 的请求体，允许透传额外字段。"""

    model_config = ConfigDict(extra="allow")

    source: str
    id: str | int
    name: str
    artist: str = ""
    album: str = ""
    pic_id: str = ""
    br: str = "999"
    lyric: bool = True
    vip: bool = False


class TrackRef(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field("", max_length=500)
    source: str = "netease"
    album: str = ""


class BackfillRequest(BaseModel):
    tracks: list[TrackRef] = Field(min_length=1, max_length=1000)
    organize: bool = True
    scan: bool = True


class PlaylistImportRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    name: str | None = Field(None, max_length=200)
    dry_run: bool = False
    loose: bool = False
    proxy: bool = False
    backfill: bool = True
    scan: bool = True


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_url = os.getenv("MUSIC_API_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("必须通过 MUSIC_API_BASE_URL 配置上游音乐 API 地址")

    app.state.client = MusicApiClient(
        base_url=base_url,
        timeout=_env_float("MUSIC_API_TIMEOUT", 30.0),
        download_timeout=_env_float("MUSIC_API_DOWNLOAD_TIMEOUT", 300.0),
        retries=_env_int("MUSIC_API_RETRIES", 2),
        cookie=os.getenv("MUSIC_API_COOKIE", ""),
        bearer_token=os.getenv("MUSIC_API_BEARER_TOKEN", ""),
        user_agent=os.getenv(
            "MUSIC_API_USER_AGENT", f"nas-music-kit-agent-api/{APP_VERSION}"
        ),
        rate_limiter=RateLimiter(
            limit=_env_int("MUSIC_API_RATE_LIMIT", 40),
            window=_env_float("MUSIC_API_RATE_WINDOW", 300.0),
        ),
    )

    navidrome: NavidromeClient | None = None
    navidrome_url = os.getenv("NAVIDROME_URL", "").strip()
    navidrome_user = os.getenv("NAVIDROME_USER", "").strip()
    navidrome_password = os.getenv("NAVIDROME_PASSWORD", "").strip()
    if navidrome_url and navidrome_user and navidrome_password:
        navidrome = NavidromeClient(
            navidrome_url,
            user=navidrome_user,
            password=navidrome_password,
            client_name=os.getenv("NAVIDROME_CLIENT", "nas-music-kit"),
            api_version=os.getenv("NAVIDROME_API_VERSION", "1.16.1"),
            timeout=_env_float("NAVIDROME_TIMEOUT", 30.0),
            scan_timeout=_env_float("NAVIDROME_SCAN_TIMEOUT", 300.0),
        )
    app.state.navidrome = navidrome
    app.state.navidrome_sync = None
    if navidrome is not None:
        app.state.navidrome_sync = NavidromeSync(
            navidrome_url,
            user=navidrome_user,
            password=navidrome_password,
            client_name=os.getenv("NAVIDROME_CLIENT", "nas-music-kit"),
            api_version=os.getenv("NAVIDROME_API_VERSION", "1.16.1"),
            timeout=_env_float("NAVIDROME_TIMEOUT", 60.0),
        )

    app.state.library_dir = os.getenv("LIBRARY_DIR", "/music")
    app.state.download_dir = os.getenv("DOWNLOAD_DIR", app.state.library_dir)
    if not os.path.isdir(app.state.library_dir):
        raise RuntimeError(f"LIBRARY_DIR 不存在: {app.state.library_dir}")
    if not os.path.isdir(app.state.download_dir):
        raise RuntimeError(f"DOWNLOAD_DIR 不存在: {app.state.download_dir}")

    app.state.jobs = JobManager(
        app.state.client,
        library_dir=app.state.library_dir,
        download_dir=app.state.download_dir,
        match_threshold=_env_float("MATCH_THRESHOLD", 0.9),
        match_source=os.getenv("MATCH_SOURCE", "netease"),
        navidrome=navidrome,
        scan_wait=_env_bool("NAVIDROME_SCAN_WAIT", True),
    )
    await app.state.jobs.start()

    yield

    await app.state.jobs.stop()
    if navidrome is not None:
        await navidrome.aclose()
    await app.state.client.aclose()


app = FastAPI(
    title="NAS Music Kit Agent API",
    version=APP_VERSION,
    lifespan=lifespan,
)


def get_client(request: Request) -> MusicApiClient:
    return request.app.state.client


@app.exception_handler(MusicApiError)
async def handle_music_api_error(_: Request, exc: MusicApiError) -> JSONResponse:
    detail: dict[str, Any] = {"error": str(exc)}
    if exc.upstream_status is not None:
        detail["upstream_status"] = exc.upstream_status
    if exc.response_body:
        detail["upstream_body"] = exc.response_body
    return JSONResponse(status_code=502, content=detail)


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit(_: Request, exc: RateLimitExceeded) -> JSONResponse:
    detail: dict[str, Any] = {"error": str(exc)}
    if exc.limit is not None:
        detail["limit"] = exc.limit
    if exc.window is not None:
        detail["window"] = exc.window
    return JSONResponse(
        status_code=429,
        content=detail,
        headers={"Retry-After": str(max(1, math.ceil(exc.retry_after)))},
    )


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "library_dir": request.app.state.library_dir,
        "download_dir": request.app.state.download_dir,
    }


@app.get("/api/search")
async def search(
    source: str = Query(..., description="音源，如 netease"),
    name: str = Query(..., description="搜索关键词"),
    pages: int = Query(1, ge=1, description="页码，从 1 开始"),
    client: MusicApiClient = Depends(get_client),
) -> Any:
    return await client.search(source=source, name=name, pages=pages)


@app.post("/api/download")
async def download(
    payload: DownloadRequest,
    client: MusicApiClient = Depends(get_client),
) -> Any:
    return await client.download(payload.model_dump(exclude_none=True))


@app.post("/api/library/backfill", status_code=202)
async def backfill(
    payload: BackfillRequest,
    request: Request,
) -> Any:
    try:
        job = request.app.state.jobs.submit(
            [track.model_dump() for track in payload.tracks],
            organize=payload.organize,
            scan=payload.scan,
        )
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "total": job["total"],
    }


@app.post("/api/playlist/import")
async def playlist_import(
    payload: PlaylistImportRequest,
    request: Request,
) -> Any:
    if payload.name and len(payload.urls) > 1:
        raise HTTPException(status_code=422, detail="多个链接时不要指定统一的 name")

    navidrome: NavidromeSync | None = request.app.state.navidrome_sync
    if navidrome is None:
        raise HTTPException(
            status_code=503,
            detail="未配置 NAVIDROME_URL / NAVIDROME_USER / NAVIDROME_PASSWORD",
        )

    proxy_url = os.getenv("IMPORT_PROXY_URL", "").strip() if payload.proxy else ""
    try:
        result = await asyncio.to_thread(
            import_playlists,
            payload.urls,
            navidrome,
            name=payload.name,
            dry_run=payload.dry_run,
            loose=payload.loose,
            proxy_url=proxy_url,
            threshold=_env_float("PLAYLIST_MATCH_THRESHOLD", 0.86),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"歌单导入失败: {exc}") from exc

    if payload.dry_run or not payload.backfill or not result["missing_tracks"]:
        return result

    job = request.app.state.jobs.submit(
        result["missing_tracks"],
        organize=True,
        scan=payload.scan,
    )
    result["backfill_job_id"] = job["job_id"]
    result["backfill_total"] = job["total"]
    return result


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> Any:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.post("/api/library/organize")
async def organize(
    request: Request,
    dry_run: bool = Query(False, description="只预览，不移动文件"),
) -> Any:
    try:
        return await asyncio.to_thread(
            organize_library,
            request.app.state.library_dir,
            source_dir=request.app.state.download_dir,
            dry_run=dry_run,
        )
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/navidrome/scan")
async def navidrome_scan(
    request: Request,
    wait: bool = Query(True, description="等待扫描完成"),
) -> Any:
    navidrome: NavidromeClient | None = request.app.state.navidrome
    if navidrome is None:
        raise HTTPException(
            status_code=503,
            detail="未配置 NAVIDROME_URL / NAVIDROME_USER / NAVIDROME_PASSWORD",
        )
    try:
        return await navidrome.start_scan(wait=wait)
    except NavidromeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
