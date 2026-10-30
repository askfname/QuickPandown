"""夸克错误码到用户可读提示的映射"""

from __future__ import annotations

_FRIENDLY_MESSAGES = {
    31001: "分享需要提取码，或提取码错误",
    41008: "分享需要提取码，或提取码错误",
    31002: "分享链接已失效或已被取消",
    41007: "分享链接已失效或已被取消",
    32003: "云盘空间不足，请清理后重试",
    23018: "触发夸克风控，请稍后重试",
}


def friendly_message(code: int, fallback: str) -> str:
    """把已知夸克错误码转成中文提示；未知错误码原样返回 fallback"""
    return _FRIENDLY_MESSAGES.get(code, fallback)
