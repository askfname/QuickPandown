"""认证 API：注册、登录、登出、当前用户"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import AuthError, validate_register

router = APIRouter(prefix="/api/auth")

COOKIE_NAME = "qp_session"


def _db_maker(request: Request) -> Any:
    return request.app.state.db_maker


def _ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return ""


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )


class RegisterPayload(BaseModel):
    username: str
    email: str
    password: str
    invite_code: str = Field(alias="inviteCode")
    captcha: str

    model_config = {"populate_by_name": True}


class LoginPayload(BaseModel):
    username: str
    password: str
    captcha: str


@router.get("/captcha")
async def captcha_image() -> Response:
    """签发验证码 SVG；id 放 HttpOnly Cookie（qp_cap），答案仅服务端留存"""
    from app import captcha

    cid, svg, _answer = captcha.new_captcha()
    resp = Response(svg, media_type="image/svg+xml")
    resp.headers["Cache-Control"] = "no-store"
    resp.set_cookie(
        "qp_cap", cid, max_age=10 * 60, httponly=True, samesite="lax", path="/"
    )
    return resp


def _check_captcha(request: Request, answer: str) -> None:
    """校验人机验证码；失败抛 400（一次性核销，错误尝试也消费）"""
    from app import captcha

    cid = request.cookies.get("qp_cap", "")
    if not captcha.verify_captcha(cid, answer):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")


@router.post("/register")
async def register(payload: RegisterPayload, request: Request, response: Response) -> dict:
    _check_captcha(request, payload.captcha)
    err = validate_register(payload.username, payload.email, payload.password, payload.invite_code)
    if err:
        raise HTTPException(status_code=400, detail=err)
    from app.auth import register as do_register

    try:
        user = await do_register(_db_maker(request), payload.username, payload.email,
                                 payload.password, payload.invite_code, ip=_ip(request))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    # 注册即登录：签发会话 Cookie（已过邀请码+验证码双验证，无二次验证尴尬）
    from app.auth import issue_session

    sess = await issue_session(_db_maker(request), user.id)
    _set_session_cookie(response, sess.token)
    return {"ok": True, "username": user.username}


@router.post("/login")
async def login_route(payload: LoginPayload, request: Request, response: Response) -> dict:
    _check_captcha(request, payload.captcha)
    from app.auth import login as do_login

    try:
        sess = await do_login(_db_maker(request), payload.username, payload.password, _ip(request))
    except AuthError as exc:
        headers = {}
        if exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        raise HTTPException(status_code=429 if exc.retry_after else 401,
                            detail=exc.message, headers=headers or None) from exc
    _set_session_cookie(response, sess.token)
    return {"ok": True, "username": payload.username}


@router.post("/logout")
async def logout_route(request: Request, response: Response) -> dict:
    from app.auth import logout as do_logout

    token = request.cookies.get(COOKIE_NAME, "")
    await do_logout(_db_maker(request), token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return {"username": user.username, "email": user.email, "role": user.role}
