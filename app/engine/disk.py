"""磁盘空间监控：用量统计与预留空间准入判断"""

from __future__ import annotations

import shutil
from pathlib import Path

MIB = 1024 * 1024


def disk_usage_bytes(path: Path) -> tuple[int, int, int]:
    """返回目录所在卷的 (total, used, free)；目录不存在时向上取最近存在目录"""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return usage.total, usage.used, usage.free


def disk_snapshot(path: Path, reserve_mb: int = 0, inflight_bytes: int = 0) -> dict:
    """磁盘用量快照：available = free - 在途任务预留，low 表示低于管理员预留值"""
    return snapshot_from_usage(*disk_usage_bytes(path), reserve_mb=reserve_mb,
                               inflight_bytes=inflight_bytes)


def snapshot_from_usage(total: int, used: int, free: int, reserve_mb: int = 0,
                        inflight_bytes: int = 0) -> dict:
    """按已取得的用量数值组装快照（便于引擎与测试复用）"""
    reserve = max(0, int(reserve_mb or 0)) * MIB
    inflight = max(0, int(inflight_bytes or 0))
    available = max(0, free - inflight)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "reserve_bytes": reserve,
        "inflight_bytes": inflight,
        "available_bytes": available,
        "low": available < reserve,
    }
