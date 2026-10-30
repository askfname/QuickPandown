"""任务与 SSE 路由：/api/download、/api/tasks、cancel/retry、/api/events。

按登录用户（request.state.user，由鉴权中间件注入）隔离；SSE 以 ?session=（=用户名）
过滤（EventSource 不能设请求头，但会话 Cookie 会自动携带、中间件把关）
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.files_api import issue_file_token
from app.config import load_settings
from app.engine.tasks import Task, TaskStatus
from app.api.routes import _user_client
from app.quark.client import QuarkApiError
from app.quark.errors import friendly_message
from app.share_session import share_session

router = APIRouter(prefix="/api")


class DownloadRequest(BaseModel):
    share_id: str
    items: list[dict]
    passcode: str = ""
    # 前端已解析出的分享标题：占位任务立即可读，不再显示裸 share_id
    title: str = ""
    # 勾选项大小表（fid → 字节）与目录叶子累计（fid → 字节）：仅作预检参考，
    # 目录体积以服务端权威统计复核（前端值可被伪造绕过）
    sizes: dict[str, int] = Field(default_factory=dict)
    dir_sizes: dict[str, int] = Field(default_factory=dict)


def _engine(request: Request) -> Any:
    return request.app.state.engine


def _settings(request: Request) -> Any:
    """引擎运行时设置优先（管理员保存即时生效），无引擎时读磁盘设置"""
    return getattr(_engine(request), "settings", None) or load_settings()


def _user(request: Request):
    return request.state.user  # 中间件保证已登录


def _serialize_tasks(tasks: list[Task], app: Any) -> list[dict]:
    """Task → JSON 字典；done 项附文件下载 URL（HMAC token）

    直接构造字典而不用 dataclasses.asdict：snapshot 已返回副本，省一次
    全量深拷贝。多文件任务发打包 token（/api/files/{token}/zip），单文件
    任务维持单文件 URL。expires_at 直接读取子项冻结的 keep_until
    """
    from app.api.files_api import issue_zip_token

    secret = getattr(app.state, "secret", b"")
    out = []
    for t in tasks:
        d = _task_dict(t)
        done_items = [i for i in d["items"] if t.status is TaskStatus.done and i["status"] == "done"]
        for item in d["items"]:
            item["file_url"] = None
        has_links = False
        if secret and done_items:
            has_links = True
            if len(done_items) == 1:
                item = done_items[0]
                token = issue_file_token(secret, t.session, t.id, item["id"])
                item["file_url"] = f"/api/files/{token}"
            else:
                token = issue_zip_token(secret, t.session, t.id, [i["id"] for i in done_items])
                d["zip_url"] = f"/api/files/{token}/zip"
        d["expires_at"] = _keep_until(t) if has_links else None
        out.append(d)
    return out


def _task_dict(task: Task) -> dict:
    """任务与其子项的 JSON 形状（字段与 dataclasses.asdict 保持一致）"""
    return {
        "id": task.id,
        "share_id": task.share_id,
        "title": task.title,
        "created_at": task.created_at,
        "status": task.status.value if isinstance(task.status, TaskStatus) else str(task.status),
        "items": [
            {
                "id": s.id,
                "name": s.name,
                "path": s.path,
                "size": s.size,
                "done_bytes": s.done_bytes,
                "status": s.status,
                "error": s.error,
                "done_at": s.done_at,
                "keep_until": s.keep_until,
            }
            for s in task.items
        ],
        "message": task.message,
        "passcode": task.passcode,
        "session": task.session,
        "terminal_at": task.terminal_at,
        "disk_low": task.disk_low,
    }


def _keep_until(task: Task) -> float | None:
    """展示/点击用的到期时刻 = 各 done 子项冻结值的最大值（持久化，不重算）。

    历史数据补不出冻结值（keep_until 为空）时返回 None——前端不展示有效期，
    下载由服务器端点的存在性校验把关
    """
    stamps = [i.keep_until for i in task.items if i.status == "done" and i.keep_until]
    return max(stamps) if stamps else None


async def _selected_bytes_authoritative(items: list[dict], *, share_id: str,
                                        passcode: str, client: Any,
                                        claimed_sizes: dict[str, int],
                                        claimed_dir_sizes: dict[str, int]) -> int:
    """勾选字节总量：文件按前端报值（转存前服务器拿不到单文件权威值），
    目录以 share_session.subtree_stats 的权威统计复核（防伪造 dir_sizes 绕过）。

    权威统计失败（分享需提取码、断网等）退回前端报值继续预检——不阻断主
    流程，引擎解析时还有一道真实叶子大小校验兜底
    """
    total = 0
    dir_fids = [str(it.get("fid")) for it in items if it.get("isDir")]
    for fid in dict.fromkeys(dir_fids):  # 去重保序
        try:
            _, bytes_total = await share_session.subtree_stats(
                share_id, fid, passcode, client=client
            )
        except (QuarkApiError, httpx.HTTPError, OSError):
            bytes_total = int(claimed_dir_sizes.get(fid, 0))
        total += bytes_total
    for it in items:
        if not it.get("isDir"):
            total += int(claimed_sizes.get(str(it.get("fid")), 0))
    return total


@router.get("/limits")
async def limits(request: Request) -> dict:
    """登录用户可读的资源限制（前端提交前按「已选」口径预检的数据源）"""
    return {"max_download_mb": getattr(_settings(request), "max_download_mb", 0)}


@router.post("/download")
async def download(payload: DownloadRequest, request: Request) -> dict:
    engine = _engine(request)
    user = _user(request)
    # 单次下载总量预检：目录体积走服务端权威统计（share_session 30 分钟 TTL 缓存，前端统计请求已拉过 → 直接命中）
    # 超限拒绝在转存前拦截，不占云端临时空间与流量，也不建任务
    limit_mb = getattr(_settings(request), "max_download_mb", 0)
    if limit_mb > 0:
        total = await _selected_bytes_authoritative(
            payload.items,
            share_id=payload.share_id,
            passcode=payload.passcode,
            client=_user_client(request),
            claimed_sizes=payload.sizes,
            claimed_dir_sizes=payload.dir_sizes,
        )
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
    live = {t.id: t for t in engine.snapshot(user.username)}
    if task_id not in live:
        # 只存在于历史（服务重启后）视为已结束；完全不存在则 404
        if task_id in {t.id for t in engine.tasks_for(user.username)}:
            raise HTTPException(status_code=400, detail="任务已结束，无需取消")
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    await engine.cancel(task_id, user.username)
    return {"ok": True}


@router.post("/tasks/{task_id}/retry")
async def retry(task_id: str, request: Request) -> dict:
    engine = _engine(request)
    user = _user(request)
    live = {t.id: t for t in engine.snapshot(user.username)}
    if task_id not in live:
        if task_id in {t.id for t in engine.tasks_for(user.username)}:
            raise HTTPException(
                status_code=400, detail="该任务已随服务重启失效，请重新提交下载"
            )
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    task = live[task_id]
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
    queue = bus.subscribe(session)

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
