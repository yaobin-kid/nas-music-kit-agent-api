from __future__ import annotations

import asyncio
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .charts_runner import ChartsBusy, ChartsNotConfigured, ChartsRunner
from .client import MusicApiClient, MusicApiError, RateLimitExceeded, RateLimiter
from .jobs import JobError, JobManager
from .library import LibraryError, organize_library
from .navidrome import NavidromeClient, NavidromeError, NavidromeSync
from .playlist_import import import_playlists
from .security import AccessPolicy, check_basic_auth, client_ip, ip_allowed, parse_networks

APP_VERSION = os.getenv("APP_VERSION", __version__)
DASHBOARD_HTML = Path(__file__).resolve().parent / "static" / "dashboard.html"


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


class ChartsRefreshRequest(BaseModel):
    dry_run: bool = False


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.started_at = time.time()
    auth_user = os.getenv("AUTH_USER", "").strip()
    auth_password = os.getenv("AUTH_PASSWORD", "")
    if bool(auth_user) != bool(auth_password):
        raise RuntimeError("AUTH_USER 和 AUTH_PASSWORD 必须同时配置")
    networks = parse_networks(os.getenv("ALLOWED_CIDRS", ""))
    raw_allowlist = os.getenv("ENABLE_IP_ALLOWLIST", "auto").strip().lower()
    if raw_allowlist in {"", "auto"}:
        ip_allowlist_enabled = bool(networks)
    elif raw_allowlist in {"1", "true", "yes", "on"}:
        ip_allowlist_enabled = True
    elif raw_allowlist in {"0", "false", "no", "off"}:
        ip_allowlist_enabled = False
    else:
        raise RuntimeError(f"ENABLE_IP_ALLOWLIST 取值无效: {raw_allowlist}")
    if ip_allowlist_enabled and not networks:
        raise RuntimeError("ENABLE_IP_ALLOWLIST 已开启，但 ALLOWED_CIDRS 为空")
    app.state.access_policy = AccessPolicy(
        networks=networks if ip_allowlist_enabled else (),
        user=auth_user,
        password=auth_password,
        trust_proxy=_env_bool("TRUST_PROXY_HEADERS", False),
    )

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
    app.state.download_dir = os.getenv("DOWNLOAD_DIR", "/music")
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

    charts_dir = os.getenv("CHARTS_DIR", "/charts")
    app.state.charts = ChartsRunner(
        charts_dir=charts_dir,
        config_path=os.getenv(
            "CHARTS_CONFIG", os.path.join(charts_dir, "config.json")
        ),
        reports_dir=os.getenv("CHARTS_REPORTS_DIR", "/tmp/charts-reports"),
        state_file=os.getenv("CHARTS_STATE_FILE", "/tmp/charts-state.json"),
        timeout=_env_float("CHARTS_TIMEOUT", 3600.0),
        python=os.getenv("CHARTS_PYTHON") or None,
    )

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


@app.middleware("http")
async def access_guard(request: Request, call_next):
    """只保护监控大盘；业务 API 不经过白名单/Basic 认证，便于 Skill 调用。"""
    path = request.url.path
    if not (
        path == "/dashboard"
        or path.startswith("/dashboard/")
        or path == "/api/dashboard/status"
    ):
        return await call_next(request)
    policy: AccessPolicy | None = getattr(request.app.state, "access_policy", None)
    if policy is None:
        return await call_next(request)
    if not ip_allowed(client_ip(request, policy.trust_proxy), policy.networks):
        return JSONResponse({"detail": "IP 不在允许范围"}, status_code=403)
    if not check_basic_auth(
        request.headers.get("authorization", ""), policy.user, policy.password
    ):
        return Response(
            content="需要登录",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="nas-music-kit"'},
            media_type="text/plain",
        )
    return await call_next(request)


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


@app.post("/api/charts/refresh")
async def charts_refresh(
    request: Request,
    payload: ChartsRefreshRequest | None = None,
    wait: bool = Query(False, description="是否等待执行结束"),
) -> Any:
    runner: ChartsRunner = request.app.state.charts
    try:
        state = await asyncio.to_thread(
            runner.refresh,
            dry_run=payload.dry_run if payload else False,
            wait=wait,
        )
    except ChartsBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ChartsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(status_code=200 if wait else 202, content=state)


@app.get("/api/charts/status")
async def charts_status(request: Request) -> Any:
    return request.app.state.charts.get_state()


@app.get("/api/charts/report")
async def charts_report(request: Request) -> Any:
    path = request.app.state.charts.latest_report()
    if path is None:
        raise HTTPException(status_code=404, detail="还没有未匹配清单")
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=path.name,
    )


@app.get("/api/dashboard/status")
async def dashboard_status(request: Request) -> Any:
    client: MusicApiClient = request.app.state.client
    return {
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - request.app.state.started_at, 1),
        "upstream": {"base_url": client.base_url, **await client.probe()},
        "rate_limit": client.rate_limit_snapshot(),
        "jobs": request.app.state.jobs.snapshot(),
        "charts": request.app.state.charts.get_state(),
        "library": {
            "library_dir": request.app.state.library_dir,
            "download_dir": request.app.state.download_dir,
        },
    }


@app.get("/", include_in_schema=False)
async def root() -> Any:
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/dashboard", include_in_schema=False)
async def dashboard_page() -> Any:
    return FileResponse(
        DASHBOARD_HTML,
        media_type="text/html; charset=utf-8",
    )


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
