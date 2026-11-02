"""注册/登录人机验证码：服务端签发 SVG 图片 + 内存单次核销。

设计：无第三方依赖；验证码答案只存服务端内存（TTL 内一次性核销），
浏览器只拿到扭曲后的 SVG（答案整串绝不明文出现在 SVG）与 HttpOnly
Cookie 里的验证码 id。错误尝试同样核销，防基于同一 id 的枚举。
签发按 IP 滑动窗口限速，避免脚本无限刷新消耗 CPU、挤占内存并淘汰
其他用户仍在有效期内的验证码
"""

from __future__ import annotations

import random
import time
from secrets import token_urlsafe

TTL_SECONDS = 300
_CHARS = "ACDEFGHJKLMNPQRTUVWXY34789"

# 错误尝试也核销；{id: (answer, expires_at)}
_store: dict[str, tuple[str, float]] = {}
# 内存硬上限：验证码签发接口可被刷，超过后淘汰最早签发项
MAX_ENTRIES = 4096

# 每 IP 每分钟最多签发 30 张，
# 脚本连刷则被 429 挡住；超过 4096 个 IP 记录后按最旧键淘汰
ISSUE_WINDOW = 60.0
ISSUE_MAX = 30
ISSUE_MAX_KEYS = 4096
_issue_hits: dict[str, list[float]] = {}


def _monotonic() -> float:
    # 可 monkeypatch 的时钟（测试隔离用）
    return time.monotonic()


def _cleanup_expired() -> None:
    now = _monotonic()
    expired = [cid for cid, (_, exp) in _store.items() if exp < now]
    for cid in expired:
        _store.pop(cid, None)


def _trim_oldest(table: dict, limit: int = ISSUE_MAX_KEYS) -> None:
    while len(table) > limit:
        table.pop(next(iter(table)), None)


def check_issue_rate(ip: str) -> int:
    """记录一次签发请求；超限返回需要等待的秒数，未超限返回 0。

    超限时不再累加时间戳，等待窗口不会因继续请求而无限延长
    """
    now = _monotonic()
    key = ip or "-"
    hits = [t for t in _issue_hits.get(key, []) if now - t < ISSUE_WINDOW]
    if len(hits) >= ISSUE_MAX:
        _issue_hits[key] = hits  # 只保留窗口内的记录，键值不会无限增长
        return max(1, int(ISSUE_WINDOW - (now - hits[0])) + 1)
    hits.append(now)
    _issue_hits[key] = hits
    if len(_issue_hits) > ISSUE_MAX_KEYS:
        # 先清过期窗口，再按最旧键淘汰；只在超限时触发，常态零成本
        for k in [k for k, v in _issue_hits.items() if not v or now - v[-1] >= ISSUE_WINDOW]:
            _issue_hits.pop(k, None)
        _trim_oldest(_issue_hits)
    return 0


def reset_issue_rate() -> None:
    """测试辅助：清空签发限速状态"""
    _issue_hits.clear()


def new_captcha() -> tuple[str, str, str]:
    """签发验证码：返回 ``(id, svg, answer)``；答案仅服务端留存"""
    _cleanup_expired()
    cid = token_urlsafe(16)
    answer = "".join(random.choice(_CHARS) for _ in range(4))
    _store[cid] = (answer, _monotonic() + TTL_SECONDS)
    while len(_store) > MAX_ENTRIES:
        _store.pop(next(iter(_store)), None)

    rng = random.Random(cid)  # 以 id 为种子：同 id 渲染稳定，无需额外状态
    glyphs: list[str] = []
    for i, ch in enumerate(answer):
        x = 22 + i * 30 + rng.randint(-3, 3)
        y = 46 + rng.randint(-4, 4)
        rot = rng.randint(-24, 24)
        color = f"hsl({rng.randint(200, 250)},55%,35%)"
        glyphs.append(
            f'<text x="{x}" y="{y}" fill="{color}" font-size="36" font-family="Consolas,monospace" '
            f'font-weight="700" transform="rotate({rot} {x} {y})">{ch}</text>'
        )
    lines = [
        f'<line x1="{rng.randint(0, 60)}" y1="{rng.randint(0, 22)}" x2="{rng.randint(90, 150)}" '
        f'y2="{rng.randint(46, 68)}" stroke="#9db4d8" stroke-width="1.5"/>'
        for _ in range(3)
    ]
    dots = [
        f'<circle cx="{rng.randint(0, 168)}" cy="{rng.randint(0, 70)}" r="{rng.uniform(0.8, 2.0):.1f}" '
        f'fill="#b9c7de"/>'
        for _ in range(36)
    ]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 168 76" width="168" height="76" '
        'role="img" aria-label="验证码">'
        '<rect width="168" height="76" fill="#eef3fb"/>'
        + "".join(dots)
        + "".join(lines)
        + "".join(glyphs)
        + "</svg>"
    )
    return cid, svg, answer


def verify_captcha(cid: str, answer: str) -> bool:
    """核销一次：正确且未过期才通过；无论对错都消费该验证码"""
    entry = _store.pop(cid or "", None)
    if entry is None:
        return False
    expect, expires_at = entry
    if expires_at < _monotonic():
        return False
    return (answer or "").strip().upper() == expect.upper()
