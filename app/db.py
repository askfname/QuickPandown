"""数据库层：连接管理、ORM 模型与建库建表（SQLAlchemy 2.0 async）。

连接串来源（优先级从高到低）：
1. 环境变量 ``QP_DB_URL``（部署脚本/容器场景）
2. 配置文件 ``data/db.json`` 的 ``db_url`` 字段（实例长期配置，迁移即带走）
3. 首次运行自动生成的 ``db.json`` 模板（默认本机 SQLite）
4. 文件损坏/字段非法：回退本机 SQLite（零依赖）
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 模板连接串：首选用 SQLite
# 需要 MySQL 的大型部署在 data/db.json 里改 db_url（格式示例看模板 _comment）
_SQLITE_COMMENT = (
    "数据库连接串来源优先级：环境变量 QP_DB_URL > 本文件 db_url"
    "MySQL 形如 mysql+aiomysql://用户名:密码@主机:3306/库名（把占位改成你的账号密码），"
    "SQLite 形如 sqlite+aiosqlite:///路径/quickpandown.db"
)


def _db_config_path():
    from app.config import DATA_DIR

    return DATA_DIR / "db.json"


def _fallback_sqlite_url() -> str:
    """SQLite 兜底连接串：文件绝对定位于 data/（不随启动 cwd 漂移）"""
    from app.config import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{DATA_DIR / 'app.sqlite3'}"


def _configured_file_url(path) -> str | None:
    """读 db.json 的 db_url；文件缺失/损坏/非法值返回 None（调用方分层决策）"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("db_url") if isinstance(data, dict) else None
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _create_config_template(path) -> None:
    """首次运行生成 db.json 模板：默认 SQLite，写入失败不阻断启动"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "db_url": _fallback_sqlite_url(),
        "_comment": _SQLITE_COMMENT,
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass


def get_db_url() -> str:
    """连接串：环境变量 > db.json > 首次生成模板（默认 SQLite）；
    文件损坏/非法值回退 SQLite"""
    env = os.environ.get("QP_DB_URL", "")
    if env.strip():
        return env.strip()

    path = _db_config_path()
    if path.exists():
        url = _configured_file_url(path)
        # 文件存在但拿不到合法值：不动用户文件，回退 SQLite
        return url or _fallback_sqlite_url()

    # 首次运行：生成模板并使用其连接串（零凭据起步）
    _create_config_template(path)
    return _fallback_sqlite_url()


def make_engine(url: str) -> AsyncEngine:
    """按后端选择连接池参数（连接长期空闲断连导致的池腐化）"""
    if url.startswith("mysql"):
        return create_async_engine(
            url,
            pool_pre_ping=True,
            # 空闲超过 1h 的连接直接弃用重连：绕开 MySQL wait_timeout 断连
            pool_recycle=3600,
        )
    return create_async_engine(url, pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[int | None] = mapped_column(default=None)  # 管理员 user.id
    used_by: Mapped[int | None] = mapped_column(default=None)  # 注册成功的 user.id
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=datetime.now)


class LoginSession(Base):
    __tablename__ = "login_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(index=True)
    expires_at: Mapped[datetime] = mapped_column()
    # 每请求滑动续期都按 last_seen 过滤/更新，在线统计同样按它扫——建索引
    last_seen: Mapped[datetime] = mapped_column(default=datetime.now, index=True)


async def init_db(url: str | None = None) -> AsyncEngine:
    """建库（MySQL）建表；幂等，重复调用无副作用。返回已建连的 engine"""
    url = url or get_db_url()
    if url.startswith("mysql"):
        # 数据库本身可能不存在，先以无库名连接创建
        base, _, db_name = url.rpartition("/")
        tmp = create_async_engine(base, isolation_level="AUTOCOMMIT")
        async with tmp.connect() as conn:
            await conn.execute(
                sa.text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4")
            )
        await tmp.dispose()
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if url.startswith("sqlite"):
            # SQLite 并发写优化：WAL（读写不互斥）+ NORMAL 同步级（断电最多丢
            # 最近事务，不损库）；连接级pragma需在回调里逐连接设置
            await conn.execute(sa.text("PRAGMA journal_mode=WAL"))
            await conn.execute(sa.text("PRAGMA synchronous=NORMAL"))
    return engine


def session_maker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def active_sessions(db, within_seconds: int = 900) -> int:
    """15 分钟内活跃的登录会话数（在线用户数）"""
    from datetime import timedelta

    from sqlalchemy import func

    cutoff = datetime.now() - timedelta(seconds=within_seconds)
    result = await db.execute(
        select(func.count()).select_from(LoginSession).where(LoginSession.last_seen >= cutoff)
    )
    return int(result.scalar_one() or 0)
