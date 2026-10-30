"""分享会话缓存：resolve stoken/title 并按 (share_id, passcode) 缓存 30 分钟"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.quark.client import QuarkApiError, QuarkClient
from app.quark.share import API_TOKEN, list_dir, walk_subtree

TTL_SECONDS = 30 * 60
# 缓存条目上限：链接数可能很多（每人每次解析都入缓存），有界防内存慢涨
MAX_ENTRIES = 256


def _now() -> float:
    return time.monotonic()


@dataclass
class SessionEntry:
    share_id: str
    stoken: str
    title: str


class ShareSession:
    """内存会话缓存；resolve 失败不写缓存，过期后自动重取，超量丢最旧"""

    MAX_ENTRIES = 256
    # 子树统计缓存条目上限（key 为 (share_id, fid)，量级与目录数同阶）
    MAX_TREE_ENTRIES = 256

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._cache: dict[tuple[str, str], tuple[SessionEntry, float]] = {}
        # (share_id, fid) → (叶子数, 字节合计, 过期时刻)；供未展开目录的选择条统计
        self._tree_cache: dict[tuple[str, str], tuple[int, int, float]] = {}

    def _evict_if_needed(self) -> None:
        """超量时先清过期，仍超则丢最旧（FIFO）"""
        if len(self._cache) <= self.MAX_ENTRIES:
            return
        now = _now()
        for key in [k for k, (_, exp) in self._cache.items() if exp < now]:
            del self._cache[key]
        while len(self._cache) > self.MAX_ENTRIES:
            oldest = next(iter(self._cache))
            del self._cache[oldest]

    def _evict_tree_cache_if_needed(self) -> None:
        """子树统计缓存：与 resolve 缓存同一淘汰策略（先清过期，再丢最旧）"""
        if len(self._tree_cache) <= self.MAX_TREE_ENTRIES:
            return
        now = _now()
        for key in [k for k, (_, _, exp) in self._tree_cache.items() if exp < now]:
            del self._tree_cache[key]
        while len(self._tree_cache) > self.MAX_TREE_ENTRIES:
            oldest = next(iter(self._tree_cache))
            del self._tree_cache[oldest]

    def set_client(self, client: Any) -> None:
        self._client = client

    async def aclose(self) -> None:
        """关闭内置兜底客户端（模块单例退出时调用）"""
        client, self._client = self._client, None
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()

    def clear(self) -> None:
        """清空全部缓存条目（测试隔离或强制重取时使用）"""
        self._cache.clear()
        self._tree_cache.clear()

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = QuarkClient()
        return self._client

    async def resolve(
        self, share_id: str, passcode: str = "", *, client: Any = None
    ) -> SessionEntry:
        """换取 stoken；传入 client 时用之（逐请求用户 Cookie），否则用内置兜底"""
        key = (share_id, passcode)
        cached = self._cache.get(key)
        if cached is not None:
            entry, expires_at = cached
            if _now() < expires_at:
                return entry
            del self._cache[key]

        use_client = client if client is not None else self._ensure_client()
        # token 端点一次返回 stoken 与分享标题（detail 端点不含标题）
        data = await use_client.request(
            "POST",
            API_TOKEN,
            json={
                "pwd_id": share_id,
                "passcode": passcode,
                "support_visit_limit_private_share": True,
            },
        )
        if not isinstance(data, dict) or not data.get("stoken"):
            raise QuarkApiError(0, "分享令牌响应缺少 stoken")

        entry = SessionEntry(
            share_id=share_id,
            stoken=str(data["stoken"]),
            title=str(data.get("title") or ""),
        )
        self._cache[key] = (entry, _now() + TTL_SECONDS)
        self._evict_if_needed()
        return entry

    async def list_dir(
        self, share_id: str, fid: str, passcode: str = "", *, client: Any = None
    ) -> list[dict]:
        """列出分享目录；client 非空时优先使用（用户 Cookie 语义）"""
        entry = await self.resolve(share_id, passcode, client=client)
        use_client = client if client is not None else self._ensure_client()
        return await list_dir(use_client, share_id, entry.stoken, fid)

    async def subtree_stats(
        self, share_id: str, fid: str, passcode: str = "", *, client: Any = None
    ) -> tuple[int, int]:
        """统计某目录子树的叶子数与字节合计（供未展开目录的选择条展示）。

        树内容在会话期内基本不变，按 (share_id, fid) 缓存 TTL_SECONDS；
        resolve 仍复用会话缓存（token 只换一次）
        """
        key = (share_id, fid)
        cached = self._tree_cache.get(key)
        if cached is not None:
            count, size, expires_at = cached
            if _now() < expires_at:
                return count, size
            del self._tree_cache[key]

        entry = await self.resolve(share_id, passcode, client=client)
        use_client = client if client is not None else self._ensure_client()
        entries = await walk_subtree(use_client, share_id, entry.stoken, fid)
        count = sum(1 for e in entries if not e.get("dir"))
        size = sum(e.get("size") or 0 for e in entries if not e.get("dir"))
        self._tree_cache[key] = (count, size, _now() + TTL_SECONDS)
        self._evict_tree_cache_if_needed()
        return count, size


# 模块级单例：供路由与后续任务（Task 8）复用
share_session = ShareSession()
