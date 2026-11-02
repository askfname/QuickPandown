"""管理员 API：邀请码管理与在线统计，仅 role=admin 可达（中间件把关）"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import DEFAULT_TRAFFIC_RESET_PERIOD, load_settings
from app.db import InviteCode

router = APIRouter(prefix="/api/admin")


def _db_maker(request: Request) -> Any:
    return request.app.state.db_maker


# 自动生成邀请码：去掉 0/O、1/I/L 等易混淆字符，按 4-4 分组便于人工转述
INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_CODE_RANDOM_LEN = 8
MAX_GENERATE_INVITES = 200


class InvitesPayload(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=200)


class GenerateInvitesPayload(BaseModel):
    count: int = Field(ge=1, le=MAX_GENERATE_INVITES)


def _new_invite_code() -> str:
    raw = "".join(
        secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_RANDOM_LEN)
    )
    return f"QP-{raw[:4]}-{raw[4:]}"


@router.post("/invites/generate")
async def generate_invites(payload: GenerateInvitesPayload, request: Request) -> dict:
    """按数量自动生成邀请码：服务端安全随机、去重后落库并返回明文"""
    user = request.state.user
    codes: list[str] = []
    async with _db_maker(request)() as db:
        existing = set(
            (await db.execute(select(InviteCode.code))).scalars()
        )
        while len(codes) < payload.count:
            code = _new_invite_code()
            if code in existing:
                continue
            existing.add(code)
            codes.append(code)
        for code in codes:
            db.add(InviteCode(code=code, created_by=user.id))
        try:
            await db.commit()
        except IntegrityError as exc:
            # 并发生成撞上唯一索引时回滚，让管理员重试即可
            await db.rollback()
            raise HTTPException(status_code=409, detail="邀请码生成冲突，请重试") from exc
    return {"ok": True, "count": len(codes), "codes": codes}



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

    from app.config import project_root
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


def _traffic_view(request: Request) -> dict:
    """流量统计快照；引擎替身/嵌入场景无计数器时返回零值，不报错"""
    tracker = getattr(request.app.state, "traffic", None)
    if tracker is not None:
        return tracker.snapshot()

    engine = getattr(request.app.state, "engine", None)
    settings = getattr(engine, "settings", None) or load_settings()
    now = time.time()
    return {
        "download_bytes": 0,
        "upload_bytes": 0,
        "total_bytes": 0,
        "period": getattr(settings, "traffic_reset_period", DEFAULT_TRAFFIC_RESET_PERIOD),
        "started_at": now,
        "next_reset_at": None,
        "updated_at": now,
    }


@router.get("/traffic")
async def traffic_stats(request: Request) -> dict:
    """管理员流量统计：上传（服务器→浏览器）与下载（夸克→服务器）文件字节"""
    return _traffic_view(request)


@router.post("/traffic/reset")
async def reset_traffic(request: Request) -> dict:
    """立即清零流量统计并开启新周期"""
    tracker = getattr(request.app.state, "traffic", None)
    if tracker is None:
        return _traffic_view(request)
    # 原子写盘很小，但放线程避免在事件循环里做文件 I/O
    return await asyncio.to_thread(tracker.reset)
