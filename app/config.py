"""应用配置与 Cookie 的本机持久化"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Optional


def project_root() -> Path:
    """返回项目根目录（app/config.py 所在目录的上一级）"""
    return Path(__file__).resolve().parent.parent


DATA_DIR = project_root() / "data"


@dataclass
class Settings:
    """用户可调整的运行时设置"""

    download_dir: str = "downloads"
    concurrency: int = 2
    temp_dir_name: str = "QuickPandownTemp"
    # 本地文件保留分钟数：每个文件的到期时刻在其下载完成瞬间用【当时】的
    # 该值定死（keep_until）并持久化，后台按冻结值到点删除；保存链路强制 ≥1
    retention_minutes: int = 60
    # 单次下载总量上限（MB）：0=不限制；按勾选文件大小总和预检，超限拒绝建任务
    max_download_mb: int = 0
    # 磁盘预留空间（MB）：可用空间低于该值时，新下载任务排队等待；0=关闭守门
    reserve_disk_mb: int = 1024

    def normalize(self) -> None:
        """负数上限/预留空间按不限制或关闭守门处理。

        保留期不做改写兜底（系统已重新部署、无 0 旧数据；保存链路与
        API 校验强制 ≥1），拿到什么用什么
        """
        if self.max_download_mb < 0:
            self.max_download_mb = 0
        if self.reserve_disk_mb < 0:
            self.reserve_disk_mb = 0


def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Optional[dict]:
    """读取 JSON 对象；文件缺失或损坏时返回 None"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    _ensure_data_dir()
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _matches_type(value: Any, expected: type) -> bool:
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def load_settings() -> Settings:
    """读取设置；文件缺失、损坏或字段非法时逐项回退默认值"""
    defaults = Settings()
    data = _read_json(_settings_path())
    if data is None:
        return defaults

    values: dict[str, Any] = {}
    for field in fields(Settings):
        value = data.get(field.name, getattr(defaults, field.name))
        expected = type(getattr(defaults, field.name))
        values[field.name] = value if _matches_type(value, expected) else getattr(defaults, field.name)
    settings = Settings(**values)
    settings.normalize()
    return settings


def save_settings(s: Settings) -> None:
    """将设置写入 data/settings.json"""
    _write_json(_settings_path(), asdict(s))
