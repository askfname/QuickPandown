"""登录链路转存引擎：空间预检、临时目录、转存轮询、直链获取与清理"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .client import UA_ELECTRON, UA_PC, QuarkApiError, QuarkClient
from .errors import friendly_message

logger = logging.getLogger(__name__)

API_SAVE = "https://drive.quark.cn/1/clouddrive/share/sharepage/save"
API_TASK = "https://drive-pc.quark.cn/1/clouddrive/task"
API_FILE = "https://drive-pc.quark.cn/1/clouddrive/file"
API_SORT = "https://drive-pc.quark.cn/1/clouddrive/file/sort"
API_DOWNLOAD = "https://drive-pc.quark.cn/1/clouddrive/file/download"
API_DELETE = "https://drive-pc.quark.cn/1/clouddrive/file/delete"
API_MEMBER = "https://drive-pc.quark.cn/1/clouddrive/member"

PAGE_SIZE = 50
SAVE_BATCH = 50
MAX_PAGES = 1000
MAX_DEPTH = 20
POLL_INTERVAL = 1.0
POLL_TIMEOUT = 120.0


@dataclass
class LinkItem:
    """可下载文件项；下载时必须携带与取直链相同的 UA。

    skip_cleanup=True 表示该 fid 是用户网盘中的原文件（自己的分享 41017
    直连路径），清理阶段必须跳过，绝不可删除
    """

    fid: str
    file_name: str
    size: int
    path: str
    download_url: str
    ua: str
    skip_cleanup: bool = False


async def free_space(client: QuarkClient) -> int:
    """查询会员容量并返回剩余字节（total - used）"""
    data = await client.request(
        "GET", API_MEMBER, params={"fetch_subscribe": "true"}
    )
    payload = data if isinstance(data, dict) else {}
    return _as_int(payload.get("total_capacity"), 0) - _as_int(
        payload.get("use_capacity"), 0
    )


async def ensure_temp_dir(client: QuarkClient, name: str, parent_fid: str = "0") -> str:
    """在 parent_fid（默认根目录）下找名为 name 的目录，找不到就创建，返回其 fid"""
    async for entry in _list_cloud_dir(client, parent_fid):
        if entry.get("dir") and entry.get("file_name") == name:
            return str(entry["fid"])

    data = await client.request(
        "POST",
        API_FILE,
        json={"pdir_fid": parent_fid, "file_name": name, "dir": True},
    )
    payload = data if isinstance(data, dict) else {}
    fid = payload.get("fid")
    if not fid:
        raise QuarkApiError(0, "创建临时目录失败：响应缺少 fid")
    return str(fid)


async def save_files(
    client: QuarkClient,
    pwd_id: str,
    stoken: str,
    items: list[dict],
    temp_fid: str,
    poll_interval: float = POLL_INTERVAL,
    poll_timeout: float = POLL_TIMEOUT,
) -> dict:
    """把分享文件分批转存到 temp_fid，轮询任务至成功，返回成功的 data"""
    last_data: Any = None
    for start in range(0, len(items), SAVE_BATCH):
        batch = items[start : start + SAVE_BATCH]
        data = await client.request(
            "POST",
            API_SAVE,
            json={
                "fid_list": [str(item.get("fid") or "") for item in batch],
                "fid_token_list": [
                    str(item.get("share_fid_token") or "") for item in batch
                ],
                "to_pdir_fid": temp_fid,
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "scene": "link",
            },
        )
        last_data = data
        task_id = _task_id(data)
        last_data = await _poll_task(
            client, task_id, poll_interval, poll_timeout
        )
    return last_data if isinstance(last_data, dict) else {}


async def temp_snapshot(
    client: QuarkClient, temp_fid: str
) -> dict[tuple[str, str, int], str]:
    """递归列临时目录，键 ``(相对路径, 文件名, size)`` → fid（与 walk_subtree 同构）"""
    snapshot: dict[tuple[str, str, int], str] = {}

    async def visit(fid: str, prefix: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise QuarkApiError(0, "临时目录嵌套过深（疑似循环）")
        async for entry in _list_cloud_dir(client, fid):
            name = str(entry.get("file_name") or "")
            path = f"{prefix}/{name}" if prefix else name
            if entry.get("dir"):
                await visit(str(entry["fid"]), path, depth + 1)
            else:
                snapshot[(path, name, _as_int(entry.get("size"), 0))] = str(
                    entry.get("fid") or ""
                )

    await visit(temp_fid, "", 0)
    return snapshot


async def get_links(client: QuarkClient, fids: list[str]) -> tuple[list[dict], str]:
    """批量取下载直链；风控 23018 时换 Electron UA 重试一次。

    返回 (links, ua)：ua 为实际产出这批直链的 User-Agent（重试后即 UA_ELECTRON），
    下载时必须带着它请求直链
    """
    body = {"fids": list(fids)}
    try:
        return (
            _as_list(await client.request("POST", API_DOWNLOAD, json=body)),
            UA_PC,
        )
    except QuarkApiError as exc:
        if exc.code != 23018:
            raise
    return (
        _as_list(
            await client.request(
                "POST",
                API_DOWNLOAD,
                json=body,
                headers={"User-Agent": UA_ELECTRON},
            )
        ),
        UA_ELECTRON,
    )


async def transfer_and_link(
    client: QuarkClient,
    share: tuple[str, str],
    items: list[dict],
    temp_fid: str,
) -> list[LinkItem]:
    """总编排：空间预检 → 转存 → 快照匹配 → 取直链 → 组装 LinkItem。

    转存报 41017（用户禁止转存自己的分享）时跳过转存：文件本就在用户
    网盘中，对原 fid 直接取直链（path 沿用原项 path）
    """
    pwd_id, stoken = share
    need = sum(_as_int(item.get("size"), 0) for item in items)
    free = await free_space(client)
    if free < need:
        raise QuarkApiError(32003, friendly_message(32003, "云盘空间不足"))

    own_share = False
    try:
        await save_files(client, pwd_id, stoken, items, temp_fid)
    except QuarkApiError as exc:
        if exc.code != 41017:
            raise
        own_share = True

    if own_share:
        links, links_ua = await get_links(client, [str(item.get("fid") or "") for item in items])
        result: list[LinkItem] = []
        for item, link in zip(items, links):
            result.append(
                LinkItem(
                    fid=str(link.get("fid") or item.get("fid") or ""),
                    file_name=str(item.get("name") or ""),
                    size=_as_int(item.get("size"), 0),
                    path=str(item.get("path") or item.get("name") or ""),
                    download_url=str(link.get("download_url") or ""),
                    ua=links_ua,
                    skip_cleanup=True,
                )
            )
        return result

    snapshot = await temp_snapshot(client, temp_fid)

    matched: dict[str, str] = {}
    for item in items:
        name = str(item.get("name") or "")
        size = _as_int(item.get("size"), 0)
        path = str(item.get("path") or name)
        fid = snapshot.get((path, name, size))
        if fid is None:
            hits = [
                snap_fid
                for (snap_path, snap_name, snap_size), snap_fid
                in snapshot.items()
                if snap_name == name and snap_size == size
            ]
            if len(hits) == 1:
                fid = hits[0]
        if fid is None:
            # 兜底：夸克对同名转存会自动改名 a.mp4 → a(1).mp4（或带空格），
            # 按去重后的规范名 + size 再匹配一次，命中唯一才接受
            base = _dedup_name(name)
            nhits = {
                snap_fid
                for (_, snap_name, snap_size), snap_fid
                in snapshot.items()
                if _dedup_name(snap_name) == base and snap_size == size
            }
            if len(nhits) == 1:
                fid = next(iter(nhits))
        if fid is None:
            # 同名同大小多份（无法区分）或快照缺失（分享者中途删文件/风控拦截）
            raise QuarkApiError(
                0,
                f"转存结果匹配失败:{name}"
                "（临时目录不存在该文件或同名同大小多份无法区分，请重试；"
                "若反复失败，请重新解析后再提交）",
            )
        matched[item.get("fid") or ""] = fid

    links, links_ua = await get_links(client, list(matched.values()))

    fid_to_link = {str(link.get("fid")): link for link in links}
    result: list[LinkItem] = []
    for item in items:
        link = fid_to_link.get(matched.get(item.get("fid") or "", ""))
        if link is None:
            raise QuarkApiError(0, f"直链响应缺少文件:{item.get('name')}")
        result.append(
            LinkItem(
                fid=str(link.get("fid") or matched.get(item.get("fid") or "", "")),
                file_name=str(item.get("name") or ""),
                size=_as_int(item.get("size"), 0),
                path=str(item.get("path") or item.get("name") or ""),
                download_url=str(link.get("download_url") or ""),
                ua=links_ua,
            )
        )
    return result


async def delete_fids(client: QuarkClient, fids: list[str]) -> bool:
    """删除云端文件；吞错仅记日志，返回是否删除成功，由调用方负责提示用户手动清理"""
    try:
        await client.request(
            "POST",
            API_DELETE,
            json={"action_type": 2, "filelist": list(fids), "exclude_fids": []},
        )
        return True
    except QuarkApiError as exc:
        logger.warning("清理临时文件失败（包络错误）: %s %s", exc.code, exc.message)
    except httpx.HTTPError as exc:
        logger.warning("清理临时文件失败（网络错误）: %s", exc)
    return False


async def _list_cloud_dir(client: QuarkClient, pdir_fid: str):
    """按 ``_size=50`` 翻页列出云盘目录，逐条产出原始 entry"""
    for page in range(1, MAX_PAGES + 1):
        data = await client.request(
            "GET",
            API_SORT,
            params={
                "pdir_fid": pdir_fid,
                "_page": page,
                "_size": PAGE_SIZE,
                "_fetch_total": 1,
            },
        )
        payload = data if isinstance(data, dict) else {}
        raw_items = payload.get("list") or []
        if not raw_items:
            return

        for raw in raw_items:
            if isinstance(raw, dict):
                yield raw

        metadata = payload.get("metadata") or {}
        total = (
            _as_int(metadata.get("_total"), -1) if isinstance(metadata, dict) else -1
        )
        if total > 0 and page * PAGE_SIZE >= total:
            return


async def _poll_task(
    client: QuarkClient,
    task_id: str,
    poll_interval: float,
    poll_timeout: float,
) -> dict:
    """轮询任务直到 status==2（成功）；status==3 抛原文错误；超时抛 QuarkApiError"""
    deadline = asyncio.get_event_loop().time() + poll_timeout
    retry_index = 0
    while True:
        data = await client.request(
            "GET", API_TASK, params={"task_id": task_id, "retry_index": retry_index}
        )
        payload = data if isinstance(data, dict) else {}
        status = _as_int(payload.get("status"), 0)
        if status == 2:
            return payload
        if status == 3:
            raise QuarkApiError(0, str(payload.get("message") or "转存任务失败"))
        if asyncio.get_event_loop().time() >= deadline:
            raise QuarkApiError(0, "转存超时")
        await asyncio.sleep(poll_interval)
        retry_index += 1


def _task_id(data: Any) -> str:
    payload = data if isinstance(data, dict) else {}
    task_id = payload.get("task_id")
    if not task_id:
        raise QuarkApiError(0, "转存响应缺少 task_id")
    return str(task_id)


def _as_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# 夸克自动改名的重复序号后缀：a(1).mp4 / a (1).mp4
_DUP_SUFFIX = re.compile(r"\s*\(\d+\)")


def _dedup_name(name: str) -> str:
    """去掉夸克自动改名的重复后缀：a(1).mp4 → a.mp4，用于转存匹配兜底"""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return _DUP_SUFFIX.sub("", name)
    return _DUP_SUFFIX.sub("", stem) + "." + ext
