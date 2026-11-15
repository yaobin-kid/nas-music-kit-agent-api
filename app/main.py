from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import __version__
from .client import MusicApiClient, MusicApiError, RateLimitExceeded, RateLimiter

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


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_url = os.getenv("MUSIC_API_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("必须通过 MUSIC_API_BASE_URL 配置上游音乐 API 地址")

    app.state.client = MusicApiClient(
        base_url=base_url,
        timeout=_env_float("MUSIC_API_TIMEOUT", 30.0),
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
    yield
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
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


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
