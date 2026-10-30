"""管理员 API：邀请码管理与在线统计，仅 role=admin 可达（中间件把关）"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import InviteCode

router = APIRouter(prefix="/api/admin")


def _db_maker(request: Request) -> Any:
    return request.app.state.db_maker


class InvitesPayload(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=200)


@router.get("/invites")
async def list_invites(request: Request) -> dict:
    async with _db_maker(request)() as db:
        rows = (await db.execute(select(InviteCode).order_by(InviteCode.id.desc()))).scalars().all()
        return {
            "invites": [
                {
                    "code": r.code,
                    "used_by": r.used_by,
                    "used_at": r.used_at.isoformat(timespec="seconds") if r.used_at else None,
                    "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
                }
                for r in rows
            ]
        }


@router.post("/invites")
async def add_invites(payload: InvitesPayload, request: Request) -> dict:
    """批量添加：跳过已存在的码，报告添加数与重复数"""
    user = request.state.user
    codes = [raw.strip() for raw in payload.codes if raw.strip()]
    added, duplicated = 0, 0
    if not codes:
        return {"ok": True, "added": 0, "duplicated": 0}
    async with _db_maker(request)() as db:
        existing = set(
            (await db.execute(select(InviteCode.code).where(InviteCode.code.in_(codes)))).scalars()
        )
        seen: set[str] = set()
        for code in codes:
            if code in existing or code in seen:
                duplicated += 1
                continue
            seen.add(code)
            db.add(InviteCode(code=code, created_by=user.id))
            added += 1
        await db.commit()
    return {"ok": True, "added": added, "duplicated": duplicated}


@router.delete("/invites")
async def remove_invites(payload: InvitesPayload, request: Request) -> dict:
    """批量删除：已使用的不可删"""
    codes = [code.strip() for code in payload.codes if code.strip()]
    removed, skipped_used = 0, 0
    if not codes:
        return {"ok": True, "removed": 0, "skipped_used": 0}
    async with _db_maker(request)() as db:
        rows = (
            await db.execute(select(InviteCode).where(InviteCode.code.in_(codes)))
        ).scalars().all()
        by_code = {inv.code: inv for inv in rows}
        for code in dict.fromkeys(codes):
            inv = by_code.get(code)
            if inv is None:
                continue
            if inv.used_by is not None:
                skipped_used += 1
                continue
            await db.delete(inv)
            removed += 1
        await db.commit()
    return {"ok": True, "removed": removed, "skipped_used": skipped_used}


@router.get("/stats")
async def stats(request: Request) -> dict:
    """在线统计：15 分钟活跃会话数 + 正在下载的去重用户数"""
    from app.db import active_sessions

    maker = _db_maker(request)
    async with maker() as db:
        online = await active_sessions(db)

    engine = request.app.state.engine
    downloading = {
        t.session
        for t in engine.snapshot()
        if t.session and t.status.value in ("transferring", "downloading")
    }
    return {"online_users": online, "downloading_users": len(downloading)}


@router.get("/disk")
async def disk_space(request: Request) -> dict:
    """磁盘监控：已用/剩余空间、预留值与当前是否触发排队"""
    engine = request.app.state.engine
    status = getattr(engine, "disk_status", None)
    if callable(status):
        try:
            return status()
        except OSError:
            pass  # 探测失败时走下面的兜底

    # 引擎替身（测试/嵌入场景）兜底：按当前设置探测下载卷
    from pathlib import Path

    from app.config import load_settings, project_root
    from app.engine.disk import disk_snapshot

    settings = getattr(engine, "settings", None) or load_settings()
    path = Path(settings.download_dir)
    if not path.is_absolute():
        path = project_root() / path
    try:
        return disk_snapshot(path, reserve_mb=int(settings.reserve_disk_mb or 0))
    except OSError:
        return {
            "total_bytes": 0, "used_bytes": 0, "free_bytes": 0,
            "reserve_bytes": int(settings.reserve_disk_mb or 0) * 1024 * 1024,
            "inflight_bytes": 0, "available_bytes": 0, "low": False,
        }
