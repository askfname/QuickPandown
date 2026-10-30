"""/api 设置路由：查看/保存设置、校验用户 Cookie（经请求头传入，不落盘）"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import MAX_RETENTION_MINUTES, load_settings, save_settings
from app.quark.account import check_cookie

router = APIRouter(prefix="/api")

INVALID_COOKIE_MESSAGE = "Cookie 无效，请重新复制"


class SettingsPayload(BaseModel):
    """全部字段可选：仅更新显式给出的项；越界值由 pydantic 拒为 422"""

    download_dir: str | None = None
    concurrency: int | None = Field(default=None, ge=1, le=10)
    # 保留期分钟数（上限 7 天：与 tasks.json 历史保留期对齐）
    retention_minutes: int | None = Field(default=None, ge=1, le=MAX_RETENTION_MINUTES)
    max_download_mb: int | None = Field(default=None, ge=0, le=1024 * 1024)
    # 磁盘预留空间（MB）：0=关闭守门
    reserve_disk_mb: int | None = Field(default=None, ge=0, le=1024 * 1024)


def _settings_view(s: Any) -> dict:
    return {
        "download_dir": s.download_dir,
        "concurrency": s.concurrency,
        "retention_minutes": s.retention_minutes,
        "max_download_mb": s.max_download_mb,
        "reserve_disk_mb": s.reserve_disk_mb,
    }


def _transport(request: Request) -> httpx.AsyncBaseTransport | None:
    return getattr(request.app.state, "transport", None)


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    """全局设置仅管理员可读（含下载目录等服务器信息）"""
    _require_admin(request)
    return _settings_view(load_settings())


def _require_admin(request: Request) -> None:
    if request.state.user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


@router.post("/settings")
async def update_settings(payload: SettingsPayload, request: Request) -> dict:
    """全局设置仅管理员可改（影响所有用户的下载目录与并发）"""
    _require_admin(request)
    settings = load_settings()
    if payload.download_dir is not None:
        settings.download_dir = payload.download_dir
    if payload.concurrency is not None:
        settings.concurrency = payload.concurrency
    if payload.retention_minutes is not None:
        settings.retention_minutes = payload.retention_minutes
    if payload.max_download_mb is not None:
        settings.max_download_mb = payload.max_download_mb
    if payload.reserve_disk_mb is not None:
        settings.reserve_disk_mb = payload.reserve_disk_mb
    save_settings(settings)

    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        engine.settings.download_dir = settings.download_dir
        engine.settings.concurrency = settings.concurrency
        engine.settings.retention_minutes = settings.retention_minutes
        engine.settings.max_download_mb = settings.max_download_mb
        engine.settings.reserve_disk_mb = settings.reserve_disk_mb
        # 保留期改动即时生效：重启清扫循环（0=停掉，>0=按新值换算间隔）
        restart = getattr(engine, "on_settings_changed", None)
        if restart is not None:
            restart()
        # 预留空间/下载目录变化：立即重评排队任务
        wake = getattr(engine, "notify_disk_changed", None)
        if wake is not None:
            await wake()
    return _settings_view(settings)


@router.post("/cookie/check")
async def check_user_cookie(request: Request) -> dict:
    """校验请求头里的用户 Cookie；轮换出的新 Cookie 经 X-Quark-Set-Cookie 头还给浏览器"""
    cookie = request.headers.get("x-quark-cookie", "")
    if not cookie.strip():
        return {"ok": False, "need_cookie": True, "message": INVALID_COOKIE_MESSAGE}

    ok, rotated = await check_cookie(cookie, transport=_transport(request))
    if not ok:
        return {"ok": False, "message": INVALID_COOKIE_MESSAGE}

    headers = {}
    if rotated and rotated != cookie:
        headers["X-Quark-Set-Cookie"] = rotated
    return _json_with_headers({"ok": True}, headers)


def _json_with_headers(content: dict, headers: dict):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=200, content=content, headers=headers)
