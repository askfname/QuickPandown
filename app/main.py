"""FastAPI 应用工厂与单例"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from app.api.files_api import load_or_create_secret
from app.api.routes import router as share_router
from app.api.settings_api import router as settings_router
from app.api.tasks_api import router as tasks_router
from app.auth_middleware import AuthMiddleware
from app.config import load_settings
from app.db import get_db_url, init_db, session_maker
from app.engine.events import EventBus
from app.engine.tasks import TaskEngine, TaskStatus
from app.quark.client import QuarkApiError, QuarkClient
from app.quark.errors import friendly_message
from app.share_session import share_session

_TERMINAL_STATUSES = {
    TaskStatus.done.value,
    TaskStatus.failed.value,
    TaskStatus.cancelled.value,
}


class OptionalStaticFiles(StaticFiles):
    """目录缺失时不报错（返回 404），目录存在后行为与 StaticFiles 一致。

    starlette 1.7 的 ``check_config`` 在请求期 stat 目录，缺目录时抛
    RuntimeError 变成 500；``check_dir=False`` 只跳过构造期检查，挡不住它
    """

    async def check_config(self) -> None:
        if not Path(str(self.directory)).exists():
            self.config_checked = True
            return
        await super().check_config()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动：建库建表 + 装历史 + 清云端临时目录 + 保留期清扫；终态任务落盘 tasks.json"""
    app.state.db_engine = await init_db(get_db_url())
    app.state.db_maker = session_maker(app.state.db_engine)
    engine: TaskEngine = app.state.engine
    app.state.history = engine.load_history()
    try:
        await engine.sweep_temp_on_startup()
    except Exception:  # noqa: BLE001 - 启动清扫失败不阻断应用
        pass

    # 本地文件保留期清扫：retention_minutes>0 才周期运行；设置变更时重启循环
    from app.api.files_api import SWEEP_INTERVAL_SECONDS, download_root, sweep_expired_files

    sweep_task: asyncio.Task | None = None

    def _start_sweeper() -> None:
        nonlocal sweep_task
        stop = asyncio.Event()

        async def loop() -> None:
            while not stop.is_set():
                # 优先读引擎运行时设置（管理员保存即时生效），无引擎回退磁盘设置
                settings = getattr(engine, "settings", None) or load_settings()
                if settings.retention_minutes > 0:
                    try:
                        sweep_expired_files(download_root(), settings.retention_minutes)
                    except Exception:  # noqa: BLE001 - 清扫失败不阻断应用
                        pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    continue

        if sweep_task is not None:
            stop.set()  # 旧循环的 wait_for 超时后自行退出
        sweep_task = asyncio.create_task(loop())
        app.state.sweeper_stop = stop

    engine.on_settings_changed = _start_sweeper  # 设置保存时换挡（间隔/开关）
    _start_sweeper()

    queue = app.state.bus.subscribe()

    async def persist_loop() -> None:
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "task" and event.get("status") in _TERMINAL_STATUSES:
                    engine.persist()
        except asyncio.CancelledError:
            pass

    persister = asyncio.create_task(persist_loop())
    try:
        yield
    finally:
        persister.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await persister
        if sweep_task is not None:
            app.state.sweeper_stop.set()
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
        app.state.bus.unsubscribe(queue)
        if app.state.db_engine is not None:
            # 连接池收尾：不 dispose 会在进程退出时留下 aiomysql
            # "Event loop is closed" 的连接泄漏尾巴
            await app.state.db_engine.dispose()


def create_app(
    transport: httpx.AsyncBaseTransport | None = None,
    engine: TaskEngine | None = None,
    bus: EventBus | None = None,
) -> FastAPI:
    """构建应用：仅建路由与挂载，不联网。

    transport 供测试注入 MockTransport；默认 None 时用真网。
    engine/bus 默认自建（真实引擎，测试注入 fakes）；数据库连接 lifespan 期建立
    """
    app = FastAPI(title="QuickPandown", lifespan=_lifespan)

    @app.exception_handler(QuarkApiError)
    async def quark_api_error_handler(_request, exc: QuarkApiError) -> JSONResponse:
        """未在路由内处理的夸克包络错误统一转 400 + 友好提示"""
        return JSONResponse(
            status_code=400,
            content={"detail": friendly_message(exc.code, exc.message or str(exc))},
        )

    client = QuarkClient(cookie="", transport=transport)
    app.state.client = client
    app.state.transport = transport
    # lifespan 前先占位（中间件可能早于 lifespan 收到请求）；真值在 lifespan 建立
    app.state.db_engine = None
    app.state.db_maker = None

    if bus is None:
        bus = EventBus()
    if engine is None:
        engine = TaskEngine(
            settings=load_settings(),
            client_factory=lambda t: QuarkClient(cookie="", transport=transport),
            share_session=share_session,
            bus=bus,
        )
    app.state.engine = engine
    app.state.bus = bus
    app.state.secret = load_or_create_secret()

    # 鉴权中间件：先于路由执行
    app.add_middleware(AuthMiddleware)

    from app.api.admin_api import router as admin_router
    from app.api.auth_api import router as auth_router

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(share_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)

    # 已完成文件下载/打包端点（HMAC token 授权）——此前漏注册为死路由，本轮修复
    from app.api.files_api import router as files_router

    app.include_router(files_router)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", OptionalStaticFiles(directory=str(static_dir), html=True, check_dir=False), name="static")
    return app


app = create_app()
