"""应用配置与 Cookie 的本机持久化"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional


def project_root() -> Path:
    """返回项目根目录（app/config.py 所在目录的上一级）"""
    return Path(__file__).resolve().parent.parent


DATA_DIR = project_root() / "data"

# 本地文件保留上限（7 天）：与 tasks.json 历史保留期对齐；历史在文件到期
# 后还会再保留一个周期，清扫器不会因历史先过期而失去文件记录
MAX_RETENTION_MINUTES = 7 * 24 * 60

# 流量统计自动归零周期：week/month/year 按固定时长计（7/30/365 天），
# never 表示只由管理员手动重置
TRAFFIC_RESET_PERIODS = ("week", "month", "year", "never")
DEFAULT_TRAFFIC_RESET_PERIOD = "month"

# 注册页邀请提示与联系方式（联系方式 icon 仅取预置值，前端内置图标）
DEFAULT_INVITE_TIP = (
    "由于服务器资源有限，目前仅限邀请用户注册。你可以通过以下方式免费获取邀请码："
)
INVITE_ICONS = ("telegram", "qq", "whatsapp", "discord", "email", "link")
MAX_INVITE_CONTACTS = 3
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DEFAULT_INVITE_CONTACTS = [
    {"name": "Telegram", "url": "https://t.me/playlab_group", "icon": "telegram"},
    {"name": "QQ 群", "url": "https://qm.qq.com/q/YpmXsCME8i", "icon": "qq"},
]


def is_valid_contact_value(icon: str, value: str) -> bool:
    """邮箱图标填邮箱地址，其余图标填 http(s) 链接"""
    value = (value or "").strip()
    if icon == "email":
        return bool(EMAIL_RE.match(value))
    return value.lower().startswith(("http://", "https://"))


def normalize_contacts(items: Any) -> list[dict[str, str]]:
    """过滤非法联系方式：预置图标、最多 MAX_INVITE_CONTACTS 条"""
    if not isinstance(items, list):
        return [dict(c) for c in DEFAULT_INVITE_CONTACTS]
    out: list[dict[str, str]] = []
    for item in items[:MAX_INVITE_CONTACTS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:32]
        url = str(item.get("url") or "").strip()[:500]
        icon = str(item.get("icon") or "link").strip()
        if icon not in INVITE_ICONS:
            icon = "link"
        if not name or not is_valid_contact_value(icon, url):
            continue
        out.append({"name": name, "url": url, "icon": icon})
    return out


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
    # 上传/下载流量统计的重置周期：week | month | year | never
    traffic_reset_period: str = DEFAULT_TRAFFIC_RESET_PERIOD
    # 注册策略：默认关闭邀请码注册（部署后可直接注册，管理员手动开启）
    invite_required: bool = False
    invite_show_tip: bool = True
    invite_tip_text: str = DEFAULT_INVITE_TIP
    invite_contacts: list[dict[str, str]] = field(
        default_factory=lambda: [dict(c) for c in DEFAULT_INVITE_CONTACTS]
    )

    def normalize(self) -> None:
        """负数上限/预留空间按不限制或关闭守门处理；保留期封顶历史保留期。"""
        if self.max_download_mb < 0:
            self.max_download_mb = 0
        if self.reserve_disk_mb < 0:
            self.reserve_disk_mb = 0
        if self.retention_minutes > MAX_RETENTION_MINUTES:
            self.retention_minutes = MAX_RETENTION_MINUTES
        if self.traffic_reset_period not in TRAFFIC_RESET_PERIODS:
            self.traffic_reset_period = DEFAULT_TRAFFIC_RESET_PERIOD
        self.invite_tip_text = (self.invite_tip_text or DEFAULT_INVITE_TIP).strip()[:500]
        self.invite_contacts = normalize_contacts(self.invite_contacts)


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
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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


def registration_policy(s: Settings | None = None) -> dict:
    """注册页可见的公开策略，不含其他服务器设置"""
    s = s or load_settings()
    return {
        "invite_required": s.invite_required,
        "show_tip": s.invite_show_tip,
        "tip_text": s.invite_tip_text,
        "contacts": [dict(c) for c in s.invite_contacts],
    }
