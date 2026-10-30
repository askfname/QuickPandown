"""已完成文件的浏览器下载端点：HMAC 签名 token 授权，按会话与任务隔离

资源约束：服务器副本按保留时长（设置项，正数分钟）由 sweep_expired_files周期清扫；
删除文件后收拢空的用户名/分享目录。链接本身有时效（token TTL），
过期访问返回 410「文件已过期，请重新下载」。多文件任务提供 ZIP 打包端点
"""

from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import struct
import tempfile
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.config import load_settings, project_root
from app.engine.paths import sanitize_rel
from app.engine.tasks import TaskStatus

router = APIRouter(prefix="/api")

TOKEN_TTL_SECONDS = 3600

# 周期清扫的节拍（间隔与保留期解耦）
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


def file_path_for(session: str, task_id: str, item_path: str) -> Path:
    """与引擎 _dest_for 同构的落盘路径（download_dir/{session}/{task_id}/...）"""
    base = Path(load_settings().download_dir)
    if not base.is_absolute():
        base = project_root() / base
    if session:
        base = base / session
    return (base / task_id).joinpath(*sanitize_rel(item_path).split("/"))


def sweep_expired_files(root: Path, tasks: "list[Any]", now: float | None = None) -> tuple[int, int]:
    """按每个文件自身冻结的到期时刻（keep_until，完成瞬间持久化）删除本地文件，
    随后把空目录（含空的用户名目录）深→浅收拢。

    tasks 为全量任务档案（内存快照 + 历史），文件按落盘
    download_dir/{session}/{task_id}/... 布局定位；目录里还有任何内容
    （含新鲜文件）就收拢不动。
    返回 (删除文件数, 删除目录数)；下载根自身从不删除
    """
    if not root.exists():
        return 0, 0
    now = now if now is not None else time.time()
    targets: set[Path] = set()
    for t in tasks or []:
        for i in t.items:
            if i.status != "done" or not i.keep_until or i.keep_until >= now:
                continue
            parts = sanitize_rel(i.path).split("/")
            sdir = root / t.session if t.session else root
            cand = (sdir / t.id).joinpath(*parts)
            try:
                if cand.is_file():
                    targets.add(cand)
            except OSError:
                pass
    removed_files = 0
    for path in targets:
        try:
            path.unlink()
            removed_files += 1
        except OSError:
            pass  # 单文件失败不阻断清扫（占用/权限问题留给下一轮）
    if not removed_files:
        return 0, 0  # 没有文件删除时无需全树遍历目录
    # 空目录收拢： deepest 优先，逐层向上尝试 rmdir（非空即停，零递归风险）
    removed_dirs = 0
    dirs = [p for p in root.rglob("*") if p.is_dir()]
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    for directory in dirs:
        try:
            directory.rmdir()  # 仅空目录成功
            removed_dirs += 1
        except OSError:
            pass
    return removed_files, removed_dirs


def download_root() -> Path:
    """解析下载根目录"""
    base = Path(load_settings().download_dir)
    if not base.is_absolute():
        base = project_root() / base
    return base


def resolve_item_file(session: str, task_id: str, item_path: str) -> Path | None:
    """解析文件真实落点（download_dir/{session}/{task_id}/...），不存在返回 None"""
    path = file_path_for(session, task_id, item_path)
    try:
        return path if path.exists() else None
    except OSError:
        return None


def _history_for(engine: Any, session: str) -> list[Any]:
    """按会话取历史；兼容只接受零参的测试引擎替身"""
    loader = getattr(engine, "load_history", None)
    if loader is None:
        return []
    try:
        return loader(session)
    except TypeError:
        return loader()


def _resolve_single_download(token: str, request: Request, session: str) -> tuple[Path, str]:
    """单文件下载的公共校验链：token 过期→410；token/任务/子项无效→404；
    文件不在盘（被保留期清扫或从未落盘）→410。GET 与 HEAD 探测共用"""
    already_late = _token_expires_at(token)
    if already_late is not None and already_late < time.time():
        raise HTTPException(status_code=410, detail="文件已过期，请重新下载")
    secret = getattr(request.app.state, "secret", b"")
    resolved = verify_file_token(secret, token, session)
    if resolved is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    task_id, item_id = resolved

    engine = request.app.state.engine
    task = next(
        (t for t in engine.snapshot(session) + _history_for(engine, session)
         if t.id == task_id and t.session == session),
        None,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    item = next((i for i in task.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")

    path = resolve_item_file(session, task.id, item.path)
    if path is None:
        # 已被保留期清扫或从未落盘：与链接过期同口径，提示重新下载
        raise HTTPException(status_code=410, detail="文件已过期，请重新下载")
    filename = sanitize_rel(item.path).split("/")[-1] or "download.bin"
    return path, filename


@router.get("/files/{token}")
async def download_file(token: str, request: Request, session: str = ""):
    """浏览器直接 GET 下载已完成文件；会话经 ?session= 校验归属，跨会话 404"""
    path, filename = _resolve_single_download(token, request, session)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.head("/files/{token}")
async def head_file(token: str, request: Request, session: str = ""):
    """前端下载前的 HEAD 存在性探测：FastAPI @get 不自动支持 HEAD，需显式注册，
    只校验不给文件体，404/410 语义与 GET 完全一致"""
    _resolve_single_download(token, request, session)
    return Response(status_code=200)


def _token_expires_at(token: str) -> float | None:
    """token 首段即过期时刻（hex 秒值）；非 token 形态返回 None（交由后续校验）"""
    try:
        return int(token.split("-", 1)[0], 16)
    except (ValueError, AttributeError):
        return None


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


def _resolve_zip_download(token: str, request: Request, session: str,
                          mark_failed: bool) -> tuple[list[tuple[object, Path]], object]:
    """打包下载的公共校验链：token 过期→410；token/任务无效→404；
    某文件不在盘→404（GET 时把任务转 failed 便于重试，HEAD 探测不动任务状态）"""
    already_late = _token_expires_at(token)
    if already_late is not None and already_late < time.time():
        raise HTTPException(status_code=410, detail="文件已过期，请重新下载")
    secret = _zip_token_secret(request)
    resolved = verify_zip_token(secret, token, session)
    if resolved is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")
    task_id, item_ids = resolved

    engine = request.app.state.engine
    task = next(
        (t for t in engine.snapshot(session) + _history_for(engine, session)
         if t.id == task_id and t.session == session),
        None,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="文件不存在或链接已过期")

    pairs: list[tuple[object, Path]] = []
    for item in task.items:
        if item.id not in item_ids or item.status != "done":
            continue
        path = resolve_item_file(session, task.id, item.path)
        if path is None:
            # 已被保留期清扫或手动删除：如实提示（mark_failed 仅真正的下载请求）
            if mark_failed:
                task.status = TaskStatus.failed
                task.message = "文件已被清理（超过保留期），请重新提交下载"
            raise HTTPException(status_code=404, detail="文件已被清理（超过保留期），请重新提交下载")
        pairs.append((item, path))

    if not pairs:
        raise HTTPException(status_code=404, detail="文件已过期，请重新下载")
    return pairs, task


@router.get("/files/{token}/zip")
async def download_zip(token: str, request: Request, session: str = ""):
    """多文件打包下载：ZIP_STORED 流式输出（不重复压缩多媒体，省 CPU）"""
    pairs, task = _resolve_zip_download(token, request, session, mark_failed=True)

    zip_name = (task.title or "download").strip() or "download"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(zip_name + '.zip')}",
    }
    return StreamingResponse(
        _zip_stream(pairs),
        media_type="application/zip",
        headers=headers,
    )


@router.head("/files/{token}/zip")
async def head_zip(token: str, request: Request, session: str = ""):
    """前端打包下载前的 HEAD 存在性探测：只校验、不给文件体，
    不动任务状态（failed 转换属真正下载请求 GET 的语义）"""
    _resolve_zip_download(token, request, session, mark_failed=False)
    return Response(status_code=200)


async def _zip_stream(pairs):
    """打包输出：ZIP_STORED（多媒体已压缩，重压缩纯耗 CPU）

    总量 ≤64MB 整包构建（内存可承受）；更大时边读边发，内存恒定
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


# ZIP32 单字段上限；超过则走临时文件 + zipfile（支持 ZIP64）
_ZIP32_LIMIT = 0xFFFFFFFF
_ZIP_CHUNK = 512 * 1024


def _dos_time(stamp: float) -> tuple[int, int]:
    """文件 mtime → DOS 时间/日期（1980–2107，越界钳制）"""
    t = time.localtime(stamp)
    year = min(max(t.tm_year, 1980), 2107)
    return (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2), (
        (year - 1980) << 9 | (t.tm_mon << 5) | t.tm_mday
    )


def _zip_name(raw: str) -> tuple[bytes, bool]:
    name = sanitize_rel(raw).replace("\\", "/")
    try:
        name.encode("ascii")
        return name.encode("ascii"), False
    except UnicodeEncodeError:
        return name.encode("utf-8"), True


async def _iter_zip(pairs):
    """真流式 ZIP：逐文件写本地头 + 数据 + 数据描述符，最后补中央目录。

    内存只保留中央目录元数据，不随文件体积增长；超过 ZIP32 上限时退回
    临时文件（zipfile 自动写 ZIP64），避免生成损坏归档
    """
    import asyncio as _asyncio

    entries = []
    total = 0
    for item, path in pairs:
        stat = path.stat()
        total += stat.st_size
        entries.append((sanitize_rel(item.path), path, stat))
    if total >= _ZIP32_LIMIT or len(entries) >= 0xFFFF or any(
        st.st_size >= _ZIP32_LIMIT for _, _, st in entries
    ):
        temp_path = _new_zip_temp()
        writer = _asyncio.create_task(
            _asyncio.to_thread(_write_zip_temp, entries, temp_path)
        )
        try:
            # shield：客户端中途断开也让写线程收尾，随后统一删除临时文件
            await _asyncio.shield(writer)
            with temp_path.open("rb") as fh:
                while True:
                    chunk = await _asyncio.to_thread(fh.read, _ZIP_CHUNK)
                    if not chunk:
                        break
                    yield chunk
        finally:
            if writer.done():
                _discard_zip_temp(writer, temp_path)
            else:
                writer.add_done_callback(lambda task: _discard_zip_temp(task, temp_path))
        return

    central: list[bytes] = []
    offset = 0
    for raw_name, path, stat in entries:
        name, utf8 = _zip_name(raw_name)
        flags = 0x08 | (0x800 if utf8 else 0)  # bit3=数据描述符, bit11=UTF-8
        mtime, mdate = _dos_time(stat.st_mtime)
        header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50, 20, flags, 0, mtime, mdate, 0, 0, 0, len(name), 0,
        )
        yield header + name
        crc = 0
        size = 0
        with path.open("rb") as fh:
            while True:
                chunk = await _asyncio.to_thread(fh.read, _ZIP_CHUNK)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
                size += len(chunk)
                yield chunk
        descriptor = struct.pack("<IIII", 0x08074B50, crc & 0xFFFFFFFF, size, size)
        yield descriptor
        central.append(
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50, 20, 20, flags, 0, mtime, mdate,
                crc & 0xFFFFFFFF, size, size,
                len(name), 0, 0, 0, 0, 0, offset,
            )
            + name
        )
        offset += len(header) + len(name) + size + len(descriptor)

    cd = b"".join(central)
    yield cd
    yield struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(cd), offset, 0
    )


def _new_zip_temp() -> Path:
    """预建临时文件路径，写线程与删除方共享同一路径，取消时也能收尾"""
    tmp = tempfile.NamedTemporaryFile(
        prefix="quickpandown-", suffix=".zip", delete=False
    )
    tmp.close()
    return Path(tmp.name)


def _write_zip_temp(entries, path: Path) -> None:
    """ZIP64 兜底：写入预建临时文件（线程内执行，由调用方删除）"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name, path_in, _stat in entries:
            zf.write(path_in, name)


def _discard_zip_temp(task: Any, path: Path) -> None:
    """取走写线程的结果并删除临时文件（成功、失败、取消统一收尾）"""
    try:
        task.exception()
    except BaseException:  # noqa: BLE001 - 异常已在别处处理或取消
        pass
    try:
        path.unlink()
    except OSError:
        pass
