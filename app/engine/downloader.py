"""流式下载器：断点续传（Range + part 文件）、有限重试与协作式取消"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import httpx

from app.quark.transfer import LinkItem

CHUNK = 512 * 1024
MAX_ATTEMPTS = 3
REFERER = "https://pan.quark.cn/"


def _default_backoff(attempt: int) -> float:
    return min(2**attempt, 8)


async def download_file(
    http: httpx.AsyncClient,
    item: LinkItem,
    cookie: str,
    dest: Path,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_flag: asyncio.Event | None = None,
    backoff: Callable[[int], float] | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> Path:
    """下载 item 到终文件全路径 dest（上层负责建目录）。

    写 ``dest.part``；已有 part 时以 ``Range: bytes=<len(part)>-`` 续传；
    整体最多 MAX_ATTEMPTS 次尝试，间隔 backoff(attempt)（默认 min(2^n,8)s）；
    取消时抛 ``asyncio.CancelledError`` 并保留 part 供下次续传。
    ``on_bytes`` 按实际收到的响应体字节回调（流量统计用，含重试）
    """
    backoff = backoff or _default_backoff
    part = dest.with_name(dest.name + ".part")
    last_error: BaseException | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        _check_cancel(cancel_flag)
        try:
            return await _attempt(
                http, item, cookie, dest, part, on_progress, cancel_flag, on_bytes
            )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            last_error = exc
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(backoff(attempt))

    raise last_error  # type: ignore[misc]


async def _attempt(
    http: httpx.AsyncClient,
    item: LinkItem,
    cookie: str,
    dest: Path,
    part: Path,
    on_progress: Callable[[int, int], None] | None,
    cancel_flag: asyncio.Event | None,
    on_bytes: Callable[[int], None] | None,
) -> Path:
    offset = part.stat().st_size if part.exists() else 0
    headers = {
        "User-Agent": item.ua,
        "Cookie": cookie,
        "Referer": REFERER,
    }
    if offset:
        headers["Range"] = f"bytes={offset}-"

    async with http.stream("GET", item.download_url, headers=headers) as response:
        response.raise_for_status()
        # 206：按断点追加；200（服务端忽略 Range 重发全部）：覆盖从头写
        if response.status_code == 206 and offset:
            mode = "ab"
            base = offset
        else:
            mode = "wb"
            base = 0
        declared = _content_length(response)
        expected = (
            declared
            if declared is not None
            else (item.size - base if item.size else None)
        )

        written = 0
        with open(part, mode) as f:
            _report(on_progress, base + written, item.size)
            async for chunk in response.aiter_bytes(CHUNK):
                _check_cancel(cancel_flag)
                f.write(chunk)
                written += len(chunk)
                if on_bytes is not None:
                    on_bytes(len(chunk))
                _check_cancel(cancel_flag)
                _report(on_progress, base + written, item.size)
        if expected is not None and written != expected:
            raise httpx.ReadError(
                f"流提前结束：收到 {written} 字节，预期 {expected} 字节"
            )

    part.replace(dest)
    return dest


def _content_length(response: httpx.Response) -> int | None:
    try:
        return int(response.headers.get("Content-Length", ""))
    except ValueError:
        return None


def _report(
    on_progress: Callable[[int, int], None] | None, done: int, total: int
) -> None:
    if on_progress is not None:
        on_progress(done, total)


def _check_cancel(cancel_flag: asyncio.Event | None) -> None:
    if cancel_flag is not None and cancel_flag.is_set():
        raise asyncio.CancelledError()
