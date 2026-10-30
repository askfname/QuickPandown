"""任务与 SSE 路由：/api/download、/api/tasks、cancel/retry、/api/events。

按登录用户（request.state.user，由鉴权中间件注入）隔离；SSE 以 ?session=（=用户名）
过滤（EventSource 不能设请求头，但会话 Cookie 会自动携带、中间件把关）
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.files_api import issue_file_token
from app.config import load_settings
from app.engine.tasks import Task, TaskStatus
from app.quark.client import QuarkApiError
from app.quark.errors import friendly_message

router = APIRouter(prefix="/api")


class DownloadRequest(BaseModel):
    share_id: str
    items: list[dict]
    passcode: str = ""
    # 前端已解析出的分享标题：占位任务立即可读，不再显示裸 share_id
    title: str = ""
    # 勾选项大小表（fid → 字节）与目录叶子累计（fid → 字节）：用于总量预检，
    # 数据源自浏览端已加载的目录树，服务器转存前即可拒绝超大请求（省流量）
    sizes: dict[str, int] = Field(default_factory=dict)
    dir_sizes: dict[str, int] = Field(default_factory=dict)


def _engine(request: Request) -> Any:
    return request.app.state.engine


def _user(request: Request):
    return request.state.user  # 中间件保证已登录


def _serialize_tasks(tasks: list[Task], app: Any) -> list[dict]:
    """dataclasses.asdict + TaskStatus 值字符串化；done 项附文件下载 URL（HMAC token）

    多文件任务发打包 token（/api/files/{token}/zip），浏览器一次拿到整个 zip；
    单文件任务维持单文件 URL
    """
    from app.api.files_api import issue_zip_token

    secret = getattr(app.state, "secret", b"")
    out = []
    for t in tasks:
        d = asdict(t)
        d["status"] = t.status.value if isinstance(t.status, TaskStatus) else str(t.status)
        done_items = [i for i in d["items"] if t.status is TaskStatus.done and i["status"] == "done"]
        for item in d["items"]:
            item["file_url"] = None
        if secret and done_items:
            if len(done_items) == 1:
                item = done_items[0]
                token = issue_file_token(secret, t.session, t.id, item["id"])
                item["file_url"] = f"/api/files/{token}"
            else:
                token = issue_zip_token(secret, t.session, t.id, [i["id"] for i in done_items])
                d["zip_url"] = f"/api/files/{token}/zip"
        out.append(d)
    return out


def _sum_selected_bytes(items: list[dict], sizes: dict[str, int],
                        dir_sizes: dict[str, int]) -> int:
    """勾选字节总量：文件按自身 size，目录按已加载叶子累计（与前端同口径）"""
    total = 0
    dir_paths = []
    for it in items:
        if it.get("isDir"):
            dir_paths.append(str(it.get("fid")))
        else:
            total += int(sizes.get(str(it.get("fid")), 0))
    if dir_paths:
        # 目录的叶子大小由前端汇总传入（dir_sizes[fid]）；未加载目录按 0 计
        for fid in dir_paths:
            total += int(dir_sizes.get(fid, 0))
        # 目录可能嵌套：dir_sizes 里已含子目录叶子，去重交给前端汇总口径
    return total


@router.post("/download")
async def download(payload: DownloadRequest, request: Request) -> dict:
    engine = _engine(request)
    user = _user(request)
    # 单次下载总量预检：超限直接拒绝（转存前拦截，不占云端临时空间与流量）
    # 读引擎运行时设置（管理员保存后立即生效），无引擎时读磁盘设置
    settings = getattr(engine, "settings", None) or load_settings()
    limit_mb = getattr(settings, "max_download_mb", 0)
    if limit_mb > 0:
        total = _sum_selected_bytes(payload.items, payload.sizes, payload.dir_sizes)
        if total > limit_mb * 1024 * 1024:
            return {
                "ok": False,
                "message": f"所选文件共 {total / (1024 * 1024):.0f}MB，超过单次下载上限 {limit_mb}MB，请减少勾选后重试",
            }
    # 夸克 Cookie 由前端经 X-Quark-Cookie 头携带（仅浏览器本地存储）
    cookie = request.headers.get("x-quark-cookie", "")
    try:
        task_id = await engine.create(
            payload.share_id,
            payload.items,
            passcode=payload.passcode,
            session=user.username,
            cookie=cookie,
            title=payload.title,
        )
    except QuarkApiError as exc:
        body: dict[str, Any] = {"ok": False, "message": friendly_message(exc.code, exc.message)}
        if exc.code == 10001 and "Cookie" in (exc.message or ""):
            body["need_cookie"] = True
        return body
    return {"ok": True, "task_id": task_id}


@router.get("/tasks")
async def tasks(request: Request) -> dict:
    engine = _engine(request)
    user = _user(request)
    return {"tasks": _serialize_tasks(engine.tasks_for(user.username), request.app)}


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str, request: Request) -> dict:
    engine = _engine(request)
    user = _user(request)
    if task_id not in {t.id for t in engine.tasks_for(user.username)}:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task_id not in {t.id for t in engine.snapshot(user.username)}:
        # 只存在于历史（服务重启后）：任务已终结，无活动可取消
        raise HTTPException(status_code=400, detail="任务已结束，无需取消")
    await engine.cancel(task_id, user.username)
    return {"ok": True}


@router.post("/tasks/{task_id}/retry")
async def retry(task_id: str, request: Request) -> dict:
    engine = _engine(request)
    user = _user(request)
    if task_id not in {t.id for t in engine.tasks_for(user.username)}:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task_id not in {t.id for t in engine.snapshot(user.username)}:
        # 只存在于历史（服务重启后）：引擎内存态已失效，重试无意义
        raise HTTPException(
            status_code=400, detail="该任务已随服务重启失效，请重新提交下载"
        )
    task = next(t for t in engine.snapshot(user.username) if t.id == task_id)
    if task.status is not TaskStatus.failed:
        raise HTTPException(
            status_code=400, detail="任务当前状态无法重试（重试仅对失败任务可用）"
        )
    if task.items and not any(s.status == "failed" for s in task.items):
        raise HTTPException(status_code=400, detail="任务没有可重试的失败文件")
    ok = await engine.retry(task_id, user.username)
    if not ok:
        raise HTTPException(status_code=400, detail="任务当前状态无法重试")
    return {"ok": True}


@router.get("/events")
async def events(request: Request, session: str = "") -> StreamingResponse:
    """SSE：只推送当前登录用户自己的事件。

    ?session= 仅作前端显式声明，服务器一律以登录身份为准——用户名可被
    他人猜测，绝不能作为订阅凭据（防跨用户事件泄漏）
    """
    user = _user(request)
    session = user.username
    bus = request.app.state.bus
    queue = bus.subscribe()

    async def stream():
        try:
            yield 'data: {"type":"hello"}\n\n'
            while True:
                event = await queue.get()
                if event.get("session") != session:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # 客户端断开（含 CancelledError）都必须退订，避免队列堆积
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
