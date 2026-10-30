"""/api 路由：链接解析与目录列表"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.quark.client import QuarkApiError, QuarkClient
from app.quark.errors import friendly_message
from app.quark.share import extract_share
from app.share_session import share_session

router = APIRouter(prefix="/api")

# 按用户 Cookie 缓存复用的夸克客户端上限（LRU）。复用连接池避免每请求
# 重新 TCP+TLS；超过上限逐出最旧的（后台 aclose 释放连接）
USER_CLIENT_CACHE_MAX = 32

# httpx 网络层异常的统一口径（不暴露 httpx/代理细节）
_NETWORK_ERROR_MESSAGE = "连接夸克服务器失败，请检查本机网络后重试"


class ParseRequest(BaseModel):
    url: str


class ListRequest(BaseModel):
    share_id: str
    fid: str
    passcode: str = ""


class PasscodeResponse(BaseModel):
    need_passcode: bool = True
    message: str


def _user_client(request: Request) -> QuarkClient:
    """带用户夸克 Cookie 的客户端（按 Cookie 缓存复用；不落服务器存储）。

    契约与单人版一致——浏览/列表使用用户自己的账号语义，避免带提取码的
    分享在游客链路上反复报"需要提取码"。缓存 key 为 Cookie 串：同一用户
    的 parse/list/目录连点共享热连接池；不同用户互不串用其 Cookie
    """
    cookie = request.headers.get("x-quark-cookie", "")
    state = request.app.state
    if not hasattr(state, "user_clients"):
        state.user_clients = OrderedDict()
    cache: OrderedDict[str, QuarkClient] = state.user_clients

    client = cache.get(cookie)
    if client is not None:
        cache.move_to_end(cookie)
        return client

    client = QuarkClient(cookie=cookie, transport=getattr(state, "transport", None))
    cache[cookie] = client
    while len(cache) > USER_CLIENT_CACHE_MAX:
        stale_key, stale = cache.popitem(last=False)
        # 后台释放底层连接；引用挂在本模块集合上防 task 未跑即被回收
        task = asyncio.create_task(stale.aclose())
        _evicted_tasks.add(task)
        task.add_done_callback(_evicted_tasks.discard)
    return client


_evicted_tasks: set[asyncio.Task] = set()


@router.post("/parse")
async def parse(payload: ParseRequest, request: Request) -> dict:
    extracted = extract_share(payload.url)
    if extracted is None:
        return PasscodeResponse(
            need_passcode=True,
            message="未识别到夸克分享链接，请粘贴包含 pan.quark.cn/s/... 的文本",
        ).model_dump()

    share_id, passcode = extracted
    client = _user_client(request)
    try:
        entry = await share_session.resolve(share_id, passcode, client=client)
        files = await share_session.list_dir(share_id, "0", passcode, client=client)
    except QuarkApiError as exc:
        if exc.code in (31001, 41008):
            return PasscodeResponse(
                need_passcode=True,
                message=friendly_message(exc.code, exc.message or "分享需要提取码"),
            ).model_dump()
        raise
    except httpx.HTTPError as exc:
        # 本地网络/代理问题：给用户可读口径，不再 5xx
        raise HTTPException(status_code=400, detail=_NETWORK_ERROR_MESSAGE) from exc

    return {"share_id": share_id, "title": entry.title, "files": files}


@router.post("/list")
async def list_files(payload: ListRequest, request: Request) -> dict:
    try:
        files = await share_session.list_dir(
            payload.share_id, payload.fid, payload.passcode, client=_user_client(request)
        )
    except QuarkApiError as exc:
        if exc.code in (31001, 41008):
            return PasscodeResponse(
                need_passcode=True,
                message=friendly_message(exc.code, exc.message or "分享需要提取码"),
            ).model_dump()
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=_NETWORK_ERROR_MESSAGE) from exc
    return {"files": files}
