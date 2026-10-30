"""启动入口

用法：
    python run.py                          # 普通启动（0.0.0.0:8756）
    python run.py --admin 用户名 邮箱 密码   # 预置管理员（已存在则跳过）
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

PORT = 8756
HOST = "0.0.0.0"

# uvicorn 优雅退出上限：浏览器页面挂着的 SSE 长连接无法被服务端优雅关闭，
# 没有超时会让第一次 Ctrl+C 停在「等待连接关闭」，用户再按一次就会强退并
# 触发退出收尾的栈噪声。给 5 秒让在途请求/任务尽量收尾
GRACEFUL_TIMEOUT = 5


class _QuietShutdownNoise(logging.Filter):
    """定向过滤 uvicorn 强退收尾的 lifespan CancelledError 栈"""
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info is not None:
            return not isinstance(record.exc_info[1], asyncio.CancelledError)
        # shutdown.failed 文本形态：uvicorn 把整段回溯当 message 记录
        return not record.getMessage().rstrip().endswith(
            "asyncio.exceptions.CancelledError"
        )


def _preset_admin(args) -> None:
    """启动命令预置管理员；幂等（已存在跳过）"""
    from app.auth import create_admin
    from app.db import get_db_url, init_db, session_maker

    async def run():
        engine = await init_db(get_db_url())
        try:
            created = await create_admin(session_maker(engine), args.admin[0], args.admin[1], args.admin[2])
        finally:
            await engine.dispose()
        print(f"管理员 {args.admin[0]}: {'已创建' if created else '已存在，跳过'}")

    asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(description="QuickPandown 服务器")
    parser.add_argument("--admin", nargs=3, metavar=("用户名", "邮箱", "密码"),
                        help="预置管理员账号（幂等），如 --admin boss boss@x.com 一个足够强的密码")
    args = parser.parse_args()
    if args.admin:
        _preset_admin(args)

    logging.getLogger("uvicorn.error").addFilter(_QuietShutdownNoise())
    try:
        uvicorn.run("app.main:app", host=HOST, port=PORT,
                    timeout_graceful_shutdown=GRACEFUL_TIMEOUT)
    except KeyboardInterrupt:
        # uvicorn 关服收尾会主动 re-raise 捕获的 SIGINT；顶层吞掉保持干净退出
        pass


if __name__ == "__main__":
    main()
