"""Cookie 有效性校验（drive-pc member 端点）"""

from __future__ import annotations

import httpx

from app.quark.client import QuarkApiError, QuarkClient

MEMBER_URL = "https://drive-pc.quark.cn/1/clouddrive/member"
MEMBER_PARAMS = {"fetch_subscribe": "true"}


async def check_cookie(
    cookie: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str]:
    """校验 Cookie：member 接口包络成功即有效。

    member 是下载链路实际依赖的登录态接口——对未登录 Cookie 返回
    401/31001（"require login [guest]"），因此它既能判真也能判假。
    （account/info 对访客同样返回 200 + data:{}，无法判别登录态，不可用）

    返回 (是否有效, 轮换后的 Cookie 串)；轮换串仅在有效且服务端下发了
    新 Cookie 时与入参不同。网络/包络异常一律视为无效（返回 False）。
    Cookie 不在服务器落盘——调用方决定轮换串的去向（当前：响应头还给浏览器）
    """
    if not cookie.strip():
        return False, cookie
    client = QuarkClient(cookie=cookie, transport=transport)
    try:
        data = await client.request("GET", MEMBER_URL, params=MEMBER_PARAMS)
    except (QuarkApiError, httpx.HTTPError):
        return False, cookie
    finally:
        await client.aclose()
    return bool(data), client.cookie
