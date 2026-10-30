"""认证服务：密码哈希、注册（邀请码一次性）、登录限速、会话生命周期"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta
from secrets import token_urlsafe

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select, update as sa_update

from app.db import InviteCode, LoginSession, User

_hasher = PasswordHasher()

SESSION_TTL = timedelta(days=7)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# 常用邮箱域名白名单（用户要求：拒绝任意域名的临时/一次性邮箱）
COMMON_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.co.jp", "live.com", "live.cn", "msn.com",
    "qq.com", "vip.qq.com", "foxmail.com",
    "163.com", "126.com", "vip.163.com", "vip.126.com",
    "sina.com", "sina.cn", "sohu.com", "aliyun.com",
    "139.com", "189.cn", "yeah.net", "icloud.com",
    "me.com", "proton.me", "zoho.com",
})

# 登录限速：连续失败 5 次后，第 n 次失败延迟 min(2^(n-5), 900) 秒
FAIL_THRESHOLD = 5
FAIL_DELAY_CAP = 900
# 内存计数：{("u", 用户名): [失败次数, 锁定到期时刻]} 与 {("i", ip): ...}
_fail_counts: dict[tuple[str, str], list] = {}

# 注册/登录接口每 IP 每分钟 10 次
RATE_WINDOW = 60.0
RATE_MAX = 10
_rate_window: dict[str, list[float]] = {}

# 内存表硬上限：唯一用户名/IP 的爆破流量不能无限占用进程内存
MAX_TRACKED_KEYS = 4096


def _trim_oldest(table: dict, limit: int = MAX_TRACKED_KEYS) -> None:
    """超过上限时丢最早插入的键（dict 保持插入序，O(溢出数)）"""
    while len(table) > limit:
        table.pop(next(iter(table)), None)


class AuthError(Exception):
    """认证业务错误；retry_after 非空表示触发限速（秒）"""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


def reset_rate_limits() -> None:
    """测试辅助：清空限速状态"""
    global _last_purge
    _fail_counts.clear()
    _rate_window.clear()
    _last_purge = 0.0


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


# Argon2 是 CPU 密集同步计算（几十到几百毫秒）：放线程池，避免阻塞事件循环
async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(verify_password, password, password_hash)


def validate_register(username: str, email: str, password: str, invite_code: str) -> str | None:
    """注册字段静态校验；返回错误消息，None 表示通过"""
    if not USERNAME_RE.match(username or ""):
        return "用户名需为 3-32 位字母、数字、下划线或中划线"
    mail = (email or "").strip()
    if not EMAIL_RE.match(mail):
        return "邮箱格式不正确"
    domain = mail.rpartition("@")[2].lower()
    if domain not in COMMON_EMAIL_DOMAINS:
        return "仅支持常用邮箱（gmail / outlook / qq / foxmail / 163 / 126 等）"
    if not password or len(password) < 8:
        return "密码至少 8 位"
    if not (invite_code or "").strip():
        return "邀请码必填"
    return None


def _check_rate(ip: str) -> None:
    """每 IP 滑动窗口限速（注册/登录共用）"""
    now = time.monotonic()
    hits = [t for t in _rate_window.get(ip, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_MAX:
        raise AuthError("请求过于频繁，请稍后再试", retry_after=int(RATE_WINDOW - (now - hits[0])) + 1)
    hits.append(now)
    _rate_window[ip] = hits
    if len(_rate_window) > MAX_TRACKED_KEYS:
        # 先清过期窗口，再按最旧键淘汰；只在超限时触发，常态零成本
        for key in [k for k, v in _rate_window.items() if not v or now - v[-1] >= RATE_WINDOW]:
            _rate_window.pop(key, None)
        _trim_oldest(_rate_window)


def _fail_delay(key: tuple[str, str]) -> int:
    """按失败次数计算本次应延迟的秒数；未达阈值返回 0。

    锁定窗口基于 locked_until（最后一次失败重新起算），被限速期间
    的尝试不重置计数——持续爆破的延迟单调递增
    """
    entry = _fail_counts.get(key)
    if not entry:
        return 0
    count, locked_until = entry
    if count < FAIL_THRESHOLD:
        return 0
    remaining = locked_until - time.monotonic()
    if remaining <= 0:
        # 锁定已过期：保留计数（半衰），下次失败继续累加而非清零
        _fail_counts[key] = [count, time.monotonic()]
        return 0
    return max(1, int(remaining))


def _record_fail(key: tuple[str, str]) -> None:
    entry = _fail_counts.get(key)
    now = time.monotonic()
    if entry:
        entry[0] += 1
        count = entry[0]
    else:
        count = 1
        _fail_counts[key] = [count, now]
    if count >= FAIL_THRESHOLD:
        # 每次失败都重新起算锁定窗口（指数递增，上限 15 分钟）
        _fail_counts[key][1] = now + min(2 ** (count - FAIL_THRESHOLD), FAIL_DELAY_CAP)
    _trim_oldest(_fail_counts)


def _clear_fail(key: tuple[str, str]) -> None:
    _fail_counts.pop(key, None)


# 未知用户名的等时验证：防时序侧信道枚举用户名
_DUMMY_HASH = _hasher.hash("timing-equalizer-dummy")


async def register(db_maker, username: str, email: str, password: str, invite_code: str, ip: str = "") -> User:
    """邀请码注册：事务内原子核销邀请码；无效/已用/重复用户名抛 AuthError"""
    _check_rate(ip)  # 注册与登录共享每 IP 限速，防邀请码爆破
    code = (invite_code or "").strip()
    async with db_maker() as db:
        # 原子占用邀请码：条件 UPDATE（used_by IS NULL 才写入），并发安全
        result = await db.execute(
            sa_update(InviteCode)
            .where(InviteCode.code == code, InviteCode.used_by.is_(None))
            .values(used_by=-1, used_at=datetime.now())  # -1 = 占位，注册成功后回填
        )
        if result.rowcount == 0:
            # 统一文案：不区分"无效"与"已用"，防邀请码枚举 oracle
            raise AuthError("邀请码无效或已被使用")
        dup = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if dup is not None:
            await db.rollback()
            raise AuthError("用户名已被注册")
        user = User(
            username=username,
            email=email,
            password_hash=await hash_password_async(password),
            role="user",
        )
        db.add(user)
        await db.flush()
        await db.execute(
            sa_update(InviteCode).where(InviteCode.code == code).values(used_by=user.id)
        )
        await db.commit()
        return user


async def login(db_maker, username: str, password: str, ip: str) -> LoginSession:
    """登录：限速检查 → 验证 → 签发会话 token。失败统一报错不泄露账号存在性"""
    _check_rate(ip)
    acct_key = ("u", username or "")
    ip_key = ("i", ip or "")
    for key in (acct_key, ip_key):
        delay = _fail_delay(key)
        if delay:
            raise AuthError(f"失败次数过多，请 {delay} 秒后重试", retry_after=delay)

    async with db_maker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            # 等时验证：未知用户名也执行一次 argon2，消除时序侧信道
            await verify_password_async(password, _DUMMY_HASH)
            _record_fail(acct_key)
            _record_fail(ip_key)
            raise AuthError("用户名或密码错误")
        if not await verify_password_async(password, user.password_hash):
            _record_fail(acct_key)
            _record_fail(ip_key)
            raise AuthError("用户名或密码错误")

        _clear_fail(acct_key)
        _clear_fail(ip_key)
        sess = LoginSession(
            token=token_urlsafe(32),
            user_id=user.id,
            expires_at=datetime.now() + SESSION_TTL,
            last_seen=datetime.now(),
        )
        db.add(sess)
        await db.commit()
        return sess


async def get_session_user(db_maker, token: str) -> User | None:
    """token 换用户；有效则滑动续期。过期/不存在返回 None。

    滑动续期节流：同一 token 5 分钟内重复请求只读不写（省一次 DB commit）；
    顺带低频清扫过期会话行（此前过期行永不删除，无限增长）
    """
    if not token:
        return None
    now = datetime.now()
    async with db_maker() as db:
        row = (
            await db.execute(
                select(LoginSession, User)
                .join(User, User.id == LoginSession.user_id)
                .where(LoginSession.token == token)
            )
        ).first()
        if row is None:
            return None
        sess, user = row
        if sess.expires_at < now:
            return None
        if (now - sess.last_seen).total_seconds() >= _RENEW_INTERVAL_SECONDS:
            sess.last_seen = now
            sess.expires_at = now + SESSION_TTL
            await _purge_if_due(db)
            await db.commit()
        return user


_RENEW_INTERVAL_SECONDS = 300
_last_purge = 0.0


async def _purge_if_due(db) -> None:
    """过期会话清扫全局限频：多用户同时续期时只执行一次 DELETE"""
    global _last_purge
    now = time.monotonic()
    if now - _last_purge < _RENEW_INTERVAL_SECONDS:
        return
    _last_purge = now
    await _purge_expired_sessions(db)


async def _purge_expired_sessions(db) -> None:
    """删除已过期的会话行（随滑动续期低频触发；结果集通常为空，代价可忽略）"""
    from sqlalchemy import delete as sa_delete

    await db.execute(sa_delete(LoginSession).where(LoginSession.expires_at < datetime.now()))


async def logout(db_maker, token: str) -> None:
    if not token:
        return
    async with db_maker() as db:
        sess = (await db.execute(select(LoginSession).where(LoginSession.token == token))).scalar_one_or_none()
        if sess is not None:
            await db.delete(sess)
            await db.commit()


async def create_admin(db_maker, username: str, email: str, password: str) -> bool:
    """启动预置管理员：已存在（按用户名）则跳过。返回是否新建"""
    async with db_maker() as db:
        dup = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if dup is not None:
            return False
        db.add(User(username=username, email=email,
                    password_hash=await hash_password_async(password), role="admin"))
        await db.commit()
        return True


async def issue_session(db_maker, user_id: int) -> LoginSession:
    """为已存在用户签发新会话 token（登录/注册成功共用，防会话固定）"""
    sess = LoginSession(
        token=token_urlsafe(32),
        user_id=user_id,
        expires_at=datetime.now() + SESSION_TTL,
        last_seen=datetime.now(),
    )
    async with db_maker() as db:
        db.add(sess)
        await db.commit()
    return sess
