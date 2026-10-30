"""夸克分享链接的免登录解析：提取、列目录、递归遍历"""

from __future__ import annotations

import re
from typing import Any

from .client import QuarkApiError, QuarkClient

API_DETAIL = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail"
API_TOKEN = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token"

PAGE_SIZE = 50
MAX_PAGES = 1000
SORT = "file_type:asc,updated_at:desc"

_SHARE_RE = re.compile(r"pan\.quark\.cn/s/([a-f0-9]{8,})")
_PASSCODE_RE = re.compile(r"[?&](?:pwd|passcode)=([A-Za-z0-9]{1,16})")


def extract_share(text: str) -> tuple[str, str] | None:
    """从任意文本提取首个分享链接的 ``(pwd_id, passcode)``；无链接返回 None。

    支持 ``?pwd=`` 与 ``?passcode=``，容忍 fragment、首尾中文文案与多条链接；
    链接没有提取码时 passcode 为空串
    """
    match = _SHARE_RE.search(text or "")
    if match is None:
        return None

    pwd_id = match.group(1)
    tail = text[match.end() :]
    # 只看首个链接自身：遇到空白或下一条链接即截断，避免串用后一条的提取码
    tail = re.split(r"\s+", tail, maxsplit=1)[0]
    tail = re.split(r"https?://", tail, maxsplit=1)[0]

    passcode_match = _PASSCODE_RE.search(tail)
    passcode = passcode_match.group(1) if passcode_match else ""
    return pwd_id, passcode


async def get_stoken(client: QuarkClient, pwd_id: str, passcode: str = "") -> str:
    """换取分享访问令牌 stoken（免登录）"""
    data = await client.request(
        "POST",
        API_TOKEN,
        json={
            "pwd_id": pwd_id,
            "passcode": passcode,
            "support_visit_limit_private_share": True,
        },
    )
    if not isinstance(data, dict) or not data.get("stoken"):
        raise QuarkApiError(0, "分享令牌响应缺少 stoken")
    return str(data["stoken"])


async def list_dir(
    client: QuarkClient,
    pwd_id: str,
    stoken: str,
    pdir_fid: str = "0",
) -> list[dict]:
    """列出一个分享目录的全部条目（自动按 ``_size=50`` 翻页）。

    每项裁剪为 ``{fid, name, dir, size, share_fid_token}``
    """
    items: list[dict] = []
    total = -1

    for page in range(1, MAX_PAGES + 1):
        data = await client.request(
            "GET",
            API_DETAIL,
            params={
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": pdir_fid,
                "force": 0,
                "_page": page,
                "_size": PAGE_SIZE,
                "_sort": SORT,
            },
        )
        payload = data if isinstance(data, dict) else {}
        raw_items = payload.get("list") or []
        if not raw_items:
            break

        for raw in raw_items:
            if isinstance(raw, dict):
                items.append(_trim_item(raw))

        if total < 0:
            metadata = payload.get("metadata") or {}
            if isinstance(metadata, dict):
                total = _as_int(metadata.get("_total"), -1)
        if total > 0 and len(items) >= total:
            break

    return items


async def walk_subtree(
    client: QuarkClient,
    pwd_id: str,
    stoken: str,
    root_fid: str = "0",
) -> list[dict]:
    """递归收集 root 下的全部目录与文件，每项附相对 root 的 ``path``"""
    result: list[dict] = []

    async def visit(fid: str, prefix: str) -> None:
        for entry in await list_dir(client, pwd_id, stoken, fid):
            path = f"{prefix}/{entry['name']}" if prefix else entry["name"]
            result.append({**entry, "path": path})
            if entry["dir"]:
                await visit(entry["fid"], path)

    await visit(root_fid, "")
    return result


def _trim_item(raw: dict) -> dict:
    """把上游 detail 列表项裁剪为前端/引擎使用的稳定结构"""
    return {
        "fid": str(raw.get("fid") or ""),
        "name": str(raw.get("file_name") or ""),
        "dir": bool(raw.get("dir")) or _as_int(raw.get("file_type"), -1) == 0,
        "size": _as_int(raw.get("size"), 0),
        "share_fid_token": str(raw.get("share_fid_token") or ""),
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
