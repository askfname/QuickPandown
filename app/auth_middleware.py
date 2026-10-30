"""全站鉴权中间件：白名单外未登录 401，管理接口非 admin 403"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# 无需登录的前缀
PUBLIC_PREFIXES = ("/api/auth/",)
# 仅管理员
ADMIN_PREFIXES = ("/api/admin/",)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 静态资源与文档放行（SPA 与登录页本身）
        is_api = path.startswith("/api/")
        if not is_api:
            return await call_next(request)

        user = await _current_user(request)
        if user is not None:
            request.state.user = user  # 公开路由也挂上身份（/api/auth/me 需要）
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "请先登录"})
        if any(path.startswith(p) for p in ADMIN_PREFIXES) and user.role != "admin":
            return JSONResponse(status_code=403, content={"detail": "需要管理员权限"})
        return await call_next(request)


async def _current_user(request: Request):
    """从会话 Cookie 还原用户；中间件内拿不到 app.state.db_maker 时返回 None"""
    token = request.cookies.get("qp_session", "")
    if not token:
        return None
    db_maker = getattr(request.app.state, "db_maker", None)
    if db_maker is None:
        return None
    from app.auth import get_session_user

    return await get_session_user(db_maker, token)
