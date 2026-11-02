"""上传/下载流量统计：内存累加、周期归零与低频原子落盘。

统计口径（只算文件本体，不含 JSON API 与页面资源）：
- 下载：服务器从夸克拉取文件时收到的响应体字节，重试重复计入；
- 上传：服务器向浏览器交付文件 / ZIP 时发出的响应体字节。

计数器在事件循环内累加，线程锁只保护跨线程读写；落盘由后台维护循环
每 30 秒最多执行一次，且仅在计数变化时写盘，空闲时零 I/O
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.config import (
    DEFAULT_TRAFFIC_RESET_PERIOD,
    TRAFFIC_RESET_PERIODS,
)

logger = logging.getLogger(__name__)

PERIOD_SECONDS = {
    "week": 7 * 24 * 3600,
    "month": 30 * 24 * 3600,
    "year": 365 * 24 * 3600,
}
# 写盘节流：管理员面板 30 秒刷新一次，落盘再慢一档可进一步减少磁盘 I/O
FLUSH_INTERVAL_SECONDS = 30.0


def normalize_period(value: Any) -> str:
    """非法/缺失周期回退默认值，保证设置文件被手改也能启动"""
    return value if value in TRAFFIC_RESET_PERIODS else DEFAULT_TRAFFIC_RESET_PERIOD


def period_seconds(period: str) -> int:
    """周期对应秒数；never 返回 0"""
    return PERIOD_SECONDS.get(period, 0)


def default_path() -> Path:
    """默认落盘位置；运行期读取 DATA_DIR，便于测试/部署重定位"""
    from app.config import DATA_DIR

    return DATA_DIR / "traffic.json"


class TrafficTracker:
    """进程内流量计数器；所有公开方法线程安全。"""

    def __init__(
        self,
        path: Path | None = None,
        period: str = DEFAULT_TRAFFIC_RESET_PERIOD,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path) if path is not None else default_path()
        self._clock = clock
        self._period = normalize_period(period)
        self._lock = threading.Lock()
        # 写盘串行化：后台循环与手动重置可能并发，保证后写入的一定是更新快照
        self._write_lock = threading.Lock()
        self._download = 0
        self._upload = 0
        self._started_at = clock()
        self._last_flush = 0.0
        self._dirty = False
        self._load()

    # ---- 计数 --------------------------------------------------------------

    def add_download(self, n: int) -> None:
        """累加服务器入站文件字节（夸克 → 服务器）"""
        self._add("_download", n)

    def add_upload(self, n: int) -> None:
        """累加服务器出站文件字节（服务器 → 浏览器）"""
        self._add("_upload", n)

    def _add(self, attr: str, n: int) -> None:
        try:
            amount = int(n)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._lock:
            setattr(self, attr, getattr(self, attr) + amount)
            self._dirty = True

    # ---- 周期 / 重置 -------------------------------------------------------

    def set_period(self, period: str) -> None:
        """应用新的重置周期；不主动清零，旧计数按新窗口重新判断是否到期"""
        value = normalize_period(period)
        with self._lock:
            if value == self._period:
                return
            self._period = value
            self._rollover_locked(self._clock())
            self._dirty = True

    def reset(self) -> dict:
        """立即清零并从当前时刻开启新周期，返回重置后的统计快照"""
        now = self._clock()
        with self._lock:
            self._download = 0
            self._upload = 0
            self._started_at = now
            self._dirty = True
        self.flush(force=True)
        return self.snapshot()

    def snapshot(self) -> dict:
        """返回当前统计（含周期起止），到期时先自动归零"""
        now = self._clock()
        with self._lock:
            self._rollover_locked(now)
            started = self._started_at
            download = self._download
            upload = self._upload
            period = self._period
        seconds = period_seconds(period)
        return {
            "download_bytes": download,
            "upload_bytes": upload,
            "total_bytes": download + upload,
            "period": period,
            "started_at": started,
            "next_reset_at": started + seconds if seconds else None,
            "updated_at": now,
        }

    def _rollover_locked(self, now: float) -> None:
        """跨过周期边界时清零，并把起点推进到最近的周期边界。

        按固定时长（7/30/365 天）滚动；停机跨过多个周期时只保留当前周期，
        并沿用原始起点对齐后续重置时刻
        """
        seconds = period_seconds(self._period)
        if not seconds:
            return
        elapsed = now - self._started_at
        if elapsed < seconds:
            return
        periods = int(elapsed // seconds)
        self._started_at += periods * seconds
        self._download = 0
        self._upload = 0
        self._dirty = True

    # ---- 持久化 ------------------------------------------------------------

    def _load(self) -> None:
        """读取上次落盘值；文件缺失、损坏或字段非法时从零开始"""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = self._clock()
        try:
            download = max(0, int(raw.get("download_bytes") or 0))
            upload = max(0, int(raw.get("upload_bytes") or 0))
            started = float(raw.get("started_at") or 0.0)
        except (TypeError, ValueError):
            return
        if started <= 0 or started > now:
            started = now
        with self._lock:
            self._download = download
            self._upload = upload
            self._started_at = started
            self._rollover_locked(now)

    def flush(self, force: bool = False) -> bool:
        """把当前计数原子写盘；返回是否真正写入。

        默认 30 秒节流；正常退出与手动重置传 force=True。写盘失败会重新
        标脏，下一轮继续尝试，绝不影响下载主流程。
        """
        now = self._clock()
        with self._write_lock:
            with self._lock:
                self._rollover_locked(now)
                if not self._dirty:
                    return False
                if not force and now - self._last_flush < FLUSH_INTERVAL_SECONDS:
                    return False
                self._dirty = False
                payload = {
                    "download_bytes": self._download,
                    "upload_bytes": self._upload,
                    "started_at": self._started_at,
                }
            try:
                self._write(payload)
            except OSError as exc:
                logger.warning("流量统计落盘失败: %s", exc)
                with self._lock:
                    self._dirty = True
                return False
            with self._lock:
                self._last_flush = now
            return True

    def _write(self, payload: dict) -> None:
        """同目录临时文件 + os.replace，避免断电留下半截 JSON"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
