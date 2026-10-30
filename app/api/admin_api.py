"""管理员 API：邀请码管理与在线统计z，仅 role=admin 可达（中间件把关）"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.db import InviteCode

router = APIRouter(prefix="/api/admin")


def _db_maker(request: Request) -> Any:
    return request.app.state.db_maker


from typing import Any  # noqa: E402


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
    added, duplicated = 0, 0
    async with _db_maker(request)() as db:
        for raw in payload.codes:
            code = raw.strip()
            if not code:
                continue
            dup = (await db.execute(select(InviteCode).where(InviteCode.code == code))).scalar_one_or_none()
            if dup is not None:
                duplicated += 1
                continue
            db.add(InviteCode(code=code, created_by=user.id))
            added += 1
        await db.commit()
    return {"ok": True, "added": added, "duplicated": duplicated}


@router.delete("/invites")
async def remove_invites(payload: InvitesPayload, request: Request) -> dict:
    """批量删除：已使用的不可删"""
    removed, skipped_used = 0, 0
    async with _db_maker(request)() as db:
        for code in payload.codes:
            inv = (await db.execute(select(InviteCode).where(InviteCode.code == code.strip()))).scalar_one_or_none()
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
