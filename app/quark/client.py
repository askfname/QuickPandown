"""夸克网盘 HTTP 客户端基座"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

import httpx

UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
UA_ELECTRON = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"


class QuarkApiError(Exception):
    """夸克接口返回的业务错误（包络 status/code 非成功）"""

    def __init__(self, code: int, message: str = "", status: int = 0) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message or f"Quark API error {code}")


def _parse_cookie(cookie: str) -> dict[str, str]:
    """把单串 Cookie 解析为有序名值对；忽略空段与无等号的段"""
    pairs: dict[str, str] = {}
    for chunk in cookie.split(";"):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs[name] = value.strip()
    return pairs


class QuarkClient:
    """持有 Cookie 与 httpx.AsyncClient 的夸克请求入口"""

    def __init__(
        self,
        cookie: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        on_cookie_updated: Callable[[str], None] | None = None,
    ) -> None:
        self.cookie = cookie
        self._on_cookie_updated = on_cookie_updated
        # trust_env=False：夸克是国内直连服务，不走系统/环境代理。否则 httpx
        # 会捡 Windows 系统代理设置，把 drive-pc.quark.cn 塞进代理隧道，
        # 节点不稳时每次连接都直接 ConnectTimeout（解析/列表/下载全灭）
        self._client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False
        )

    @property
    def base_headers(self) -> dict[str, str]:
        """默认请求头（每次返回新 dict，调用方可安全修改）"""
        return {
            "User-Agent": UA_PC,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
        }

    def gparams(self) -> dict[str, Any]:
        """公共 query 参数；每次调用重新生成随机数与时间戳"""
        return {
            "pr": "ucpro",
            "fr": "pc",
            "__dt": random.randint(200, 9999),
            "__t": int(time.time() * 1000),
        }

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | list | None = None,
        headers: dict | None = None,
    ) -> Any:
        """发起请求、吸收 Set-Cookie 并校验包络，成功时返回包络中的 data"""
        query = self.gparams()
        if params:
            query.update(params)

        merged_headers = self.base_headers
        if headers:
            merged_headers.update(headers)
        if self.cookie:
            merged_headers["Cookie"] = self.cookie

        response = await self._client.request(
            method,
            url,
            params=query,
            json=json,
            headers=merged_headers,
        )
        self._absorb_set_cookie(response.headers)

        try:
            payload = response.json()
        except ValueError as exc:
            raise QuarkApiError(0, f"响应不是合法 JSON: {exc}", response.status_code) from exc
        if not isinstance(payload, dict):
            raise QuarkApiError(0, "响应包络格式非法", response.status_code)

        status = payload.get("status")
        code = payload.get("code")
        if status == 200 and code == 0:
            return payload.get("data")

        raise QuarkApiError(
            _as_int(code, 0),
            str(payload.get("message") or ""),
            _as_int(status, response.status_code),
        )

    async def aclose(self) -> None:
        """释放底层 httpx.AsyncClient"""
        await self._client.aclose()

    def _absorb_set_cookie(self, headers: httpx.Headers) -> None:
        """把响应 Set-Cookie 的 name=value 合并回 self.cookie，变化时回调"""
        current = _parse_cookie(self.cookie)
        merged = dict(current)
        for raw in headers.get_list("set-cookie"):
            pair = raw.split(";", 1)[0].strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            name = name.strip()
            if not name:
                continue
            merged[name] = value.strip()

        if merged == current:
            return
        self.cookie = "; ".join(f"{name}={value}" for name, value in merged.items())
        if self._on_cookie_updated is not None:
            self._on_cookie_updated(self.cookie)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
