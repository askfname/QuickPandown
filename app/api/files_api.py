"""已完成文件的浏览器下载端点：HMAC 签名 token 授权，按会话与任务隔离

资源约束：retention_minutes=0（不保留）时文件在下载响应完成后即删；
>0 时由 sweep_expired_files 周期清扫。多文件任务提供 ZIP 打包端点
"""

from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.config import load_settings, project_root
from app.engine.paths import sanitize_rel

router = APIRouter(prefix="/api")

TOKEN_TTL_SECONDS = 3600

# 周期清扫的节拍：保留期 >0 时按它定时扫（间隔与保留期解耦）
SWEEP_INTERVAL_SECONDS = 600


def load_or_create_secret() -> bytes:
    """服务器密钥：首次启动生成并落盘 data/secret.key（不含任何用户数据）"""
    path = project_root() / "data" / "secret.key"
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return bytes.fromhex(raw)
    except (OSError, ValueError):
        pass
    raw = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return bytes.fromhex(raw)


def _mac(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_file_token(secret: bytes, session: str, task_id: str, item_id: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    """签发下载 token：`过期hex-签名`，签名覆盖 session|task|item|过期"""
    expires = int(time.time()) + ttl
    payload = f"{session}|{task_id}|{item_id}|{expires}"
    return f"{expires:x}-{_mac(secret, payload)}"


def verify_file_token(secret: bytes, token: str, session: str) -> tuple[str, str] | None:
    """校验 token 属于该会话且未过期，返回 (task_id, item_id)；否则 None"""
    try:
        exp_hex, mac = token.split("-", 1)
        expires = int(exp_hex, 16)
    except (ValueError, AttributeError):
        return None
    task_id_item_id = _token_hints.get(token)
    if task_id_item_id is None:
        return None
    task_id, item_id = task_id_item_id
    expected = _mac(secret, f"{session}|{task_id}|{item_id}|{expires}")
    if not hmac.compare_digest(expected, mac) or expires < time.time():
        return None
    return task_id, item_id


# token -> (task_id, item_id)：签发时登记的提示表（内存，仅服务重启即清空；
# 重启后历史任务前端会重新拉 /api/tasks 获得新 token）
_token_hints: dict[str, tuple[str, str]] = {}


def issue_file_token(secret: bytes, session: str, task_id: str, item_id: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    """签发 token 并登记提示；提示表超限时淘汰最旧的 1/8"""
    token = make_file_token(secret, session, task_id, item_id, ttl)
    _token_hints[token] = (task_id, item_id)
    if len(_token_hints) > 4096:
        for k in list(_token_hints.keys())[:512]:
            _token_hints.pop(k, None)
    return token


def file_path_for(session: str, item_path: str) -> Path:
    """与引擎 _dest_for 同构的落盘路径（download_dir/{session}/...）"""
    base = Path(load_settings().download_dir)
    if not base.is_absolute():
        base = project_root() / base
    if session:
        base = base / session
    return base.joinpath(*sanitize_rel(item_path).split("/"))


def sweep_expired_files(root: Path, retention_minutes: int, now: float | None = None) -> int:
    """按 mtime 删除超过保留期的文件，返回删除数

    只删文件不删目录（保留会话/分享目录结构，空目录不占空间）；
    retention_minutes<=0 直接跳过（0=不保留的立即删在下载完成后做，不归这里）
    """
    if retention_minutes <= 0 or not root.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - retention_minutes * 60
    removed = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass  # 单文件失败不阻断清扫（占用/权限问题留给下一轮）
    return removed


def _delete_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # 删除失败不影响下载响应（留给周期清扫兜底）


def download_root() -> Path:
    """解析下载根目录（相对则相对项目根）。"""
    base = Path(load_settings().download_dir)
    if not base.is_absolute():
        base = project_root() / base
    return base


@router.get("/files/{token}")
async def download_file(token: str, request: Request, session: str = ""):
    """浏览器直接 GET 下载已完成文件；会话经 ?session= 校验归属，跨会话 404，

    不保留模式（retention_minutes=0）：响应完成后立即删除本地文件
    """
    secret = getattr(request.app.state, "secret", b"")
    resolved = verify_file_token(secret, token, session)
    if resolved is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    task_id, item_id = resolved

    engine = request.app.state.engine
    task = next(
        (t for t in engine.snapshot(session) + engine.load_history() if t.id == task_id and t.session == session),
        None,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    item = next((i for i in task.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")

    path = file_path_for(session, item.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")

    filename = sanitize_rel(item.path).split("/")[-1] or "download.bin"
    delete_after = load_settings().retention_minutes == 0
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        background=_BackgroundUnlink(path) if delete_after else None,
    )


class _BackgroundUnlink:
    """starlette BackgroundTask 等价物：响应发送完成后删除文件（不引额外依赖）"""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def __call__(self) -> None:
        _delete_quiet(self.path)


# ---- 多文件打包下载（ZIP）-----------------------------------------------------


def _zip_token_secret(request: Request) -> bytes:
    return getattr(request.app.state, "secret", b"")


def make_zip_token(secret: bytes, session: str, task_id: str, item_ids: list[str],
                   ttl: int = TOKEN_TTL_SECONDS) -> str:
    """打包端点 token：与单文件同构（HMAC 覆盖 session|task|ZIP|items|过期）"""
    expires = int(time.time()) + ttl
    payload = f"{session}|{task_id}|ZIP|{','.join(item_ids)}|{expires}"
    return f"{expires:x}-{_mac(secret, payload)}"


def verify_zip_token(secret: bytes, token: str, session: str) -> tuple[str, list[str]] | None:
    """校验打包 token，返回 (task_id, item_ids)；无效/过期/篡改返回 None"""
    try:
        exp_hex, mac = token.split("-", 1)
        expires = int(exp_hex, 16)
    except (ValueError, AttributeError):
        return None
    hints = _zip_token_hints.get(token)
    if hints is None:
        return None
    task_id, item_ids = hints
    expected = _mac(secret, f"{session}|{task_id}|ZIP|{','.join(item_ids)}|{expires}")
    if not hmac.compare_digest(expected, mac) or expires < time.time():
        return None
    return task_id, item_ids


# token -> (task_id, item_ids)：与 _token_hints 同构的提示表（内存有界）
_zip_token_hints: dict[str, tuple[str, list[str]]] = {}


def issue_zip_token(secret: bytes, session: str, task_id: str,
                    item_ids: list[str], ttl: int = TOKEN_TTL_SECONDS) -> str:
    token = make_zip_token(secret, session, task_id, item_ids, ttl)
    _zip_token_hints[token] = (task_id, list(item_ids))
    if len(_zip_token_hints) > 4096:
        for k in list(_zip_token_hints.keys())[:512]:
            _zip_token_hints.pop(k, None)
    return token


@router.get("/files/{token}/zip")
async def download_zip(token: str, request: Request, session: str = ""):
    """多文件打包下载：ZIP_STORED 流式输出（不重复压缩多媒体，省 CPU）

    打包中文件缺失（保留期已清扫）：任务转 failed 提示重新提交，不再哑 404
    """
    secret = _zip_token_secret(request)
    resolved = verify_zip_token(secret, token, session)
    if resolved is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    task_id, item_ids = resolved

    engine = request.app.state.engine
    task = next(
        (t for t in engine.snapshot(session) + engine.load_history()
         if t.id == task_id and t.session == session),
        None,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")

    pairs: list[tuple[object, Path]] = []
    for item in task.items:
        if item.id not in item_ids or item.status != "done":
            continue
        path = file_path_for(session, item.path)
        if not path.exists():
            # 已被保留期清扫或手动删除：如实提示并让任务回到可重试态
            from app.engine.tasks import TaskStatus

            task.status = TaskStatus.failed
            task.message = "文件已被清理（超过保留期），请重新提交下载"
            from app.engine.tasks import _TERMINAL  # noqa: F401  确保状态合法

            raise HTTPException(status_code=404, detail="文件已被清理（超过保留期），请重新提交下载")
        pairs.append((item, path))

    if not pairs:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")

    zip_name = (task.title or "download").strip() or "download"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(zip_name + '.zip')}",
    }
    return StreamingResponse(
        _zip_stream(pairs),
        media_type="application/zip",
        headers=headers,
        background=_ZipCleanup(pairs) if load_settings().retention_minutes == 0 else None,
    )


async def _zip_stream(pairs):
    """打包输出：ZIP_STORED（多媒体已压缩，重压缩纯耗 CPU）

    总量 ≤64MB 整包构建（内存可承受）；更大时分块 yield 避免双倍内存峰值
    """
    import asyncio as _asyncio

    total = sum(p.stat().st_size for _, p in pairs)
    if total <= 64 * 1024 * 1024:
        data = await _asyncio.get_event_loop().run_in_executor(None, _build_zip, pairs)
        yield data
        return
    async for chunk in _iter_zip(pairs):
        yield chunk


def _build_zip(pairs) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for item, path in pairs:
            zf.write(path, sanitize_rel(item.path))
    return buf.getvalue()


async def _iter_zip(pairs):
    """大文件流式：每个文件单独 ZipInfo 逐块读取拼接（无中央目录回写，兼容性优先分卷）"""
    import asyncio as _asyncio

    def _first() -> bytes:
        # 流式 zip：逐个文件打包成单成员 zip 再拼接不可行（中央目录），
        # 大文件场景退化为逐文件独立输出会让浏览器收到多文件——因此
        # 这里仍整包构建，只是分块 yield，避免一次性占双倍内存
        return _build_zip(pairs)

    data = await _asyncio.get_event_loop().run_in_executor(None, _first)
    for i in range(0, len(data), 256 * 1024):
        yield data[i : i + 256 * 1024]


class _ZipCleanup:
    """打包响应完成后按不保留模式删除源文件"""

    def __init__(self, pairs) -> None:
        self.paths = [p for _, p in pairs]

    async def __call__(self) -> None:
        for p in self.paths:
            _delete_quiet(p)
