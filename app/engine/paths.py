"""落盘路径清理：Windows 非法字符、结尾点/空格与保留名"""

from __future__ import annotations

_ILLEGAL = '<>:"/\\|?*'
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}


def sanitize(name: str) -> str:
    """把单级目录名/文件名清理为 Windows 合法：非法字符与结尾点/空格替换为 ``_``，保留名整体替换为 ``_``"""
    cleaned = "".join("_" if ch in _ILLEGAL else ch for ch in name)
    body = cleaned.rstrip(". ")
    cleaned = body + "_" * (len(cleaned) - len(body))
    if not cleaned or cleaned.upper() in _RESERVED:
        return "_"
    return cleaned


def sanitize_rel(path: str) -> str:
    """按 ``/`` 分段逐段 sanitize 后重拼，空段剔除"""
    return "/".join(sanitize(seg) for seg in path.split("/") if seg)
