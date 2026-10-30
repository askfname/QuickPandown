"""下载任务引擎：任务状态机、并发下载、取消清理、重试、启动清扫与历史持久化"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import Settings, project_root
from app.engine.downloader import download_file
from app.engine.paths import sanitize_rel
from app.quark import transfer
from app.quark.client import QuarkApiError
from app.quark.share import walk_subtree
from app.quark.transfer import LinkItem

logger = logging.getLogger(__name__)

NO_COOKIE_CODE = 10001
NO_COOKIE_MESSAGE = "需要先设置夸克 Cookie"

# 终态任务内存保留时长：与下载 token TTL 同量级；过期后从 _tasks 修剪，
# UI 与下载链接仍能经 tasks.json 历史寻址（tasks_for 会合并历史）。failed 不修剪
#（retry 需要 Cookie 与待重试子项）。长跑进程里终态任务不再无限堆积
TERMINAL_TTL_SECONDS = 3600
# 终态任务的 tasks.json 历史保留期：更老的条目在 persist 落盘/载入时修剪，
# 防 tasks.json 随时间无限增长
HISTORY_RETENTION_SECONDS = 7 * 24 * 3600
# 进度事件节流间隔：on_progress 每个 HTTP 块都会回调，SSE 全量转发会让
# 总线/前端被高频事件淹没；每 (task, 子项) 至多 ~0.3s 推一次（完成必推）
PROGRESS_INTERVAL_SECONDS = 0.3


class TaskStatus(str, Enum):
    queued = "queued"
    transferring = "transferring"
    downloading = "downloading"
    cleaning = "cleaning"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


_TERMINAL = {TaskStatus.done, TaskStatus.failed, TaskStatus.cancelled}


@dataclass
class SubItem:
    id: str
    name: str
    path: str
    size: int
    done_bytes: int = 0
    status: str = "waiting"
    error: str | None = None
    # 下载完成时刻（time.time()）：进度档案与排查依据；持久化进 tasks.json
    done_at: float | None = None
    # 冻结的本地到期时刻：下载完成瞬间用【当时】的保留时长算出并持久化。
    # 之后调整保留时长只影响此后新下载的文件，旧文件的有效期不再漂移
    keep_until: float | None = None


@dataclass
class Task:
    id: str
    share_id: str
    title: str
    created_at: float
    status: TaskStatus
    items: list[SubItem] = field(default_factory=list)
    message: str = ""
    passcode: str = ""
    session: str = ""
    # 进入终态的时间戳（time.time()）：终态内存修剪与历史保留期的依据，
    # 随任务持久化进 tasks.json；老数据缺失时由历史的 created_at 回退
    terminal_at: float | None = None


class TaskEngine:
    """编排 转存→下载→清理 的内存任务表；终态任务可持久化为 tasks.json。

    转存清单与直链按 (task_id, sub_id) 存在引擎运行时字典里，不进入 Task/SubItem
    数据类，持久化与 snapshot 因此天然干净。任务按 session（浏览器会话键）隔离
    """

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[httpx.AsyncBaseTransport | None], Any],
        share_session: Any,
        bus: Any,
        cookie_getter: Callable[[], str] | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._share_session = share_session
        self._bus = bus
        self._cookie_getter = cookie_getter or (lambda: "")
        self._db_path = db_path or project_root() / "data" / "tasks.json"
        self._tasks: dict[str, Task] = {}
        self._runners: dict[str, asyncio.Task] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        # task_id -> 后台目录解析协程（create 后台化；解析完成前 run 尚未启动）
        self._preparers: dict[str, asyncio.Task] = {}
        # task_id -> 原始选择 [{fid, isDir}]：后台解析与失败重试的输入
        self._pending: dict[str, list[dict]] = {}
        # task_id -> 有序 (sub_id, walk 叶子 dict)；转存入参与 SubItem 的对应关系
        self._leaves: dict[str, list[tuple[str, dict]]] = {}
        # task_id -> sub_id -> 转存后拿到的直链
        self._links: dict[str, dict[str, LinkItem]] = {}
        # task_id -> 用户 Cookie（仅内存；任务终态即清，绝不进持久化）
        # failed 任务保留 Cookie 供 retry 使用，任务离开内存（persist 后从 _tasks
        # 移除不在本期实现，见 plan）时随进程结束清空
        self._cookies: dict[str, str] = {}
        # (task_id, sub_id) -> 上次进度事件 time.monotonic()：SSE 进度节流
        self._progress_ts: dict[tuple[str, str], float] = {}

    # ---- 创建 / 查询 -------------------------------------------------------

    async def create(
        self,
        share_id: str,
        items: list[dict],
        passcode: str = "",
        session: str = "",
        cookie: str = "",
        title: str = "",
    ) -> str:
        """先建占位任务入队并立即返回 task_id；目录解析（resolve+walk，重网络）后台进行。

        无 Cookie 仍同步拒绝（零副作用）；解析期错误（分享失效/无文件等）
        转成 failed 任务（可重试），而不是让 /api/download 挂到解析完成
        """
        if not cookie:
            raise QuarkApiError(NO_COOKIE_CODE, NO_COOKIE_MESSAGE)

        self._prune_terminal()  # 借新任务入场顺带修剪过期终态（有任务才扫，零额外周期）

        task = Task(
            id=uuid.uuid4().hex[:12],
            share_id=share_id,
            # 标题先占位（前端传入的已知标题优先），解析成功后回填真实标题
            title=title or share_id,
            created_at=time.time(),
            status=TaskStatus.queued,
            message="正在解析目录…",
            passcode=passcode,
            session=session,
            items=[],
        )
        self._tasks[task.id] = task
        self._cancel_flags[task.id] = asyncio.Event()
        self._pending[task.id] = [dict(i) for i in items]
        self._cookies[task.id] = cookie
        self._publish_task(task)
        self._prepare(task.id, cookie)
        return task.id

    def _prepare(self, task_id: str, cookie: str) -> None:
        """目录解析放后台协程：create/retry 立即返回，不阻塞 HTTP 应答"""
        preparer = asyncio.create_task(self._prepare_run(task_id, cookie))
        self._preparers[task_id] = preparer

    async def _prepare_run(self, task_id: str, cookie: str) -> None:
        """后台目录解析：resolve+walk_subtree 补齐明细后启动 run() 下载主流程"""
        task = self._tasks.get(task_id)
        flag = self._cancel_flags.get(task_id)
        if task is None or flag is None:
            return
        try:
            if flag.is_set() or task.status is not TaskStatus.queued:
                raise asyncio.CancelledError()
            client = self._client_factory(None)
            client.cookie = cookie
            items = self._pending.get(task_id) or []
            entry = await self._share_session.resolve(task.share_id, task.passcode)
            if flag.is_set() or task.status is not TaskStatus.queued:
                raise asyncio.CancelledError()
            tree = await walk_subtree(client, task.share_id, entry.stoken, "0")
            if flag.is_set() or task.status is not TaskStatus.queued:
                raise asyncio.CancelledError()

            by_fid = {str(e["fid"]): e for e in tree}
            wanted = {str(i["fid"]) for i in items}
            dir_paths = {
                str(by_fid[fid]["path"]) for fid in wanted if fid in by_fid and by_fid[fid].get("dir")
            }
            whole_share = "0" in wanted
            leaves = [
                e
                for e in tree
                if not e["dir"]
                and (whole_share or str(e["fid"]) in wanted or _under_any(str(e["path"]), dir_paths))
            ]
            if not leaves:
                raise QuarkApiError(0, "未找到可下载的文件")

            task.title = entry.title or task.title
            task.items = [
                SubItem(
                    id="it" + uuid.uuid4().hex[:8],
                    name=str(leaf["name"]),
                    path=str(leaf["path"]),
                    size=int(leaf.get("size") or 0),
                )
                for leaf in leaves
            ]
            self._leaves[task.id] = [(sub.id, leaf) for sub, leaf in zip(task.items, leaves)]
            self._pending.pop(task.id, None)

            # 服务端最后一道权威校验：前端大小表可被伪造绕过，树已 walk 到真实叶子大小，
            # 超限任务直接失败——不转存不下载，零云端临时空间零流量
            limit_mb = int(self.settings.max_download_mb or 0)
            if limit_mb > 0:
                total_bytes = sum(sub.size for sub in task.items)
                if total_bytes > limit_mb * 1024 * 1024:
                    raise QuarkApiError(
                        0,
                        f"所选文件共 {total_bytes / (1024 * 1024):.0f}MB，"
                        f"超过单次下载上限 {limit_mb}MB，请减少勾选后重试",
                    )

            self._preparers.pop(task_id, None)
            self._start(task.id, cookie)
        except asyncio.CancelledError:
            self._preparers.pop(task_id, None)
            # 解析期取消：run() 尚未启动，这里自行收尾（cancel() 已收尾则不重复）
            if task.status is TaskStatus.queued:
                for sub in task.items:
                    sub.status = "cancelled"
                    self._publish_item(task, sub)
                await self._cleanup(task)
                self._cleanup_local(task)
                self._finish(task, TaskStatus.cancelled, "已取消")
        except QuarkApiError as exc:
            self._preparers.pop(task_id, None)
            self._finish_fail_preparing(task, exc.message or str(exc))
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都落到 failed
            self._preparers.pop(task_id, None)
            self._finish_fail_preparing(task, str(exc) or exc.__class__.__name__)

    def _finish_fail_preparing(self, task: Task, message: str) -> None:
        """解析期失败收尾：转 failed 可重试；_pending 保留供 retry 重新解析"""
        if task.status is not TaskStatus.queued:
            return  # 已被取消收尾，不覆盖终态
        self._finish(task, TaskStatus.failed, message)

    def snapshot(self, session: str | None = None) -> list[Task]:
        """返回任务的深拷贝（可选按会话过滤），避免 UI 读取时与引擎内部状态竞态"""
        return [
            Task(
                id=t.id,
                share_id=t.share_id,
                title=t.title,
                created_at=t.created_at,
                status=t.status,
                items=[replace(s) for s in t.items],
                message=t.message,
                passcode=t.passcode,
                session=t.session,
            )
            for t in self._tasks.values()
            if session is None or t.session == session
        ]

    def tasks_for(self, session: str) -> list[Task]:
        """本会话的全部任务：内存活任务 + 历史合并（去重，活的优先）"""
        live = {t.id: t for t in self.snapshot(session)}
        merged: list[Task] = list(live.values())
        for h in self.load_history():
            if h.session == session and h.id not in live:
                merged.append(h)
        return merged

    # ---- 运行 / 取消 / 重试 ------------------------------------------------

    async def run(self, task_id: str) -> None:
        """单任务主流程：transferring → downloading → cleaning → done/failed/cancelled"""
        task = self._tasks.get(task_id)
        if task is None or task.status is not TaskStatus.queued:
            return

        flag = self._cancel_flags[task_id]
        try:
            if flag.is_set():
                raise asyncio.CancelledError()
            await self._transfer(task, flag)
            if flag.is_set():
                raise asyncio.CancelledError()

            self._set_status(task, TaskStatus.downloading, "")
            await self._download_all(task, flag)

            failed = [s for s in task.items if s.status == "failed"]
            if failed:
                await self._cleanup(task)
                self._finish(
                    task,
                    TaskStatus.failed,
                    f"{len(failed)} 个文件下载失败：{failed[0].error or ''}",
                )
                return

            self._set_status(task, TaskStatus.cleaning, "正在清理云端临时文件…")
            cleanup_ok = await self._cleanup(task)
            if cleanup_ok:
                self._finish(task, TaskStatus.done, "")
            else:
                self._finish(
                    task,
                    TaskStatus.done,
                    "下载完成，但云端临时文件删除失败，请手动删除网盘 QuickPandownTemp 目录中的文件",
                )
        except asyncio.CancelledError:
            for sub in task.items:
                if sub.status in ("waiting", "downloading"):
                    sub.status = "cancelled"
                    self._publish_item(task, sub)
            await self._cleanup(task)
            self._cleanup_local(task)  # 未完成文件不留在盘上（done 文件保留交付）
            self._finish(task, TaskStatus.cancelled, "已取消")
        except QuarkApiError as exc:
            await self._cleanup(task)
            self._finish(task, TaskStatus.failed, exc.message or str(exc))
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都落到 failed
            await self._cleanup(task)
            self._finish(task, TaskStatus.failed, str(exc) or exc.__class__.__name__)
        finally:
            self._leaves.pop(task_id, None)

    async def cancel(self, task_id: str, session: str | None = None) -> None:
        """置取消标志；运行中的任务由 run() 走清理后标 cancelled，排队未启动的直接清理标 cancelled。

        session 非 None 时校验归属（不匹配直接返回，路由层转 404）
        """
        task = self._tasks.get(task_id)
        if task is None or task.status in _TERMINAL:
            return
        if session is not None and task.session != session:
            return

        flag = self._cancel_flags.get(task_id)
        if flag is not None:
            flag.set()

        # 排队任务（runner 尚未跑过任何转存）：直接标 cancelled 并清理，
        # run() 开头的标志检查会让迟到的 runner 立即返回
        if task.status is TaskStatus.queued:
            for sub in task.items:
                sub.status = "cancelled"
                self._publish_item(task, sub)
            await self._cleanup(task)
            self._cleanup_local(task)
            self._finish(task, TaskStatus.cancelled, "已取消")
            return

        runner = self._runners.get(task_id)
        if runner is not None:
            # shield：wait_for 超时只放弃等待，不取消任务本身（标志位仍会让它自行收尾）
            try:
                await asyncio.wait_for(asyncio.shield(runner), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            return

        await self._cleanup(task)
        self._cleanup_local(task)
        self._finish(task, TaskStatus.cancelled, "已取消")

    async def retry(self, task_id: str, session: str | None = None) -> bool:
        """仅对 failed 子项从"拿直链"阶段重跑（重新走 transfer_and_link 主流程）。

        session 非 None 时校验归属（不匹配返回 False，路由层转 404）
        """
        task = self._tasks.get(task_id)
        if task is None or task.status is not TaskStatus.failed:
            return False
        if session is not None and task.session != session:
            return False

        pending = self._pending.get(task_id)
        if pending is not None:
            # 解析期失败（尚无明细）：重试 = 重新后台解析，保留原始选择
            task.status = TaskStatus.queued
            task.message = "正在解析目录…"
            self._publish_task(task)
            self._prepare(task_id, self._cookie_for(task))
            return True

        retry_subs = [s for s in task.items if s.status == "failed"]
        if not retry_subs:
            return False

        client = self._client_factory(None)
        client.cookie = self._cookie_for(task)
        entry = await self._share_session.resolve(task.share_id, task.passcode)
        tree = await walk_subtree(client, task.share_id, entry.stoken, "0")
        by_path = {str(e["path"]): e for e in tree if not e["dir"]}
        pairs = [(s.id, by_path[s.path]) for s in retry_subs if s.path in by_path]
        if not pairs:
            return False

        for sub in retry_subs:
            sub.status = "waiting"
            sub.error = None
            sub.done_bytes = 0
        task.status = TaskStatus.queued
        task.message = ""
        self._leaves[task_id] = pairs
        self._publish_task(task)
        self._start(task_id, self._cookie_for(task))
        return True

    # ---- 启动清扫 ----------------------------------------------------------

    async def sweep_temp_on_startup(self) -> int:
        """列空 QuickPandownTemp 并按 fid 删除，返回删除的文件条目数"""
        client = self._client_factory(None)
        temp_fid = await transfer.ensure_temp_dir(client, self.settings.temp_dir_name)
        snapshot = await transfer.temp_snapshot(client, temp_fid)
        fids = [fid for fid in snapshot.values() if fid]
        if fids:
            await transfer.delete_fids(client, fids)
        return len(fids)

    # ---- 持久化 ------------------------------------------------------------

    def persist(self) -> None:
        """把终态任务 upsert 进 db_path（按 id 合并既有历史，重启后不丢旧记录）

        落盘同时修剪超过 HISTORY_RETENTION_SECONDS 的超期历史（终态任务）：
        tasks.json 不随时间无限增长；最近条目与本次终态任务正常写入
        """
        existing: dict[str, dict] = {}
        try:
            data = json.loads(self._db_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for raw in data.get("tasks") or []:
                    if (isinstance(raw, dict) and raw.get("id")
                            and not _history_expired(raw)):
                        existing[str(raw["id"])] = raw
        except (OSError, ValueError):
            existing = {}  # 文件缺失或损坏：视作空历史

        for task in self._tasks.values():
            if task.status in _TERMINAL:
                existing[task.id] = _task_to_dict(task)

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path.write_text(
            json.dumps({"tasks": list(existing.values())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_history(self) -> list[Task]:
        """从 db_path 读历史任务；过期条目与损坏条目一并跳过（文件缺失逐条回退）"""
        try:
            data = json.loads(self._db_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        tasks: list[Task] = []
        now = time.time()
        for raw in data.get("tasks") or []:
            if not isinstance(raw, dict) or _history_expired(raw, now):
                continue
            try:
                tasks.append(_task_from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return tasks

    def freeze_history_expiry(self) -> int:
        """历史档案一次性补齐冻结到期时刻（keep_until），启动时执行。

        已有 keep_until 的子项跳过；可按 done_at 或落盘文件 mtime 推算的用
        【当时】保留时长计算并写回 tasks.json
        """
        try:
            data = json.loads(self._db_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        window = max(1, self.settings.retention_minutes) * 60
        changed = 0
        for raw in data.get("tasks") or []:
            if not isinstance(raw, dict):
                continue
            session = str(raw.get("session") or "")
            for item in raw.get("items") or []:
                if not isinstance(item, dict) or item.get("status") != "done":
                    continue
                if "keep_until" in item:
                    continue
                stamp = float(item.get("done_at") or 0.0)
                if not stamp:
                    stamp = self._item_file_mtime(
                        session, str(raw.get("id") or ""), str(item.get("path") or "")
                    )
                item["keep_until"] = stamp + window if stamp else None
                changed += 1
        if changed:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path.write_text(
                json.dumps({"tasks": data.get("tasks") or []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return changed

    def _item_file_mtime(self, session: str, task_id: str, rel: str) -> float:
        """done 子项落盘文件的 mtime（download_dir/{session}/{task_id}/... 布局），查不到返回 0.0"""
        try:
            parts = sanitize_rel(rel).split("/")
        except ValueError:
            return 0.0
        base = self._download_base()
        sdir = base / session if session else base
        cand = (sdir / task_id).joinpath(*parts)
        try:
            if cand.is_file():
                return cand.stat().st_mtime
        except OSError:
            pass
        return 0.0

    # ---- 内部 --------------------------------------------------------------

    async def _transfer(self, task: Task, flag: asyncio.Event) -> None:
        """转存并取直链；LinkItem 按 (sub_id, leaf) 顺序映射回 SubItem"""
        self._set_status(task, TaskStatus.transferring, "正在转存…")
        pairs = self._leaves.get(task.id)
        if not pairs:
            raise QuarkApiError(0, "缺少转存清单")

        cookie_client = self._client_factory(None)
        cookie_client.cookie = self._cookie_for(task)
        entry = await self._share_session.resolve(task.share_id, task.passcode)
        # 两级临时目录：先确保 QuickPandownTemp 父目录存在，再在其中建会话子目录
        parent_fid = await transfer.ensure_temp_dir(cookie_client, self.settings.temp_dir_name)
        temp_fid = await transfer.ensure_temp_dir(
            cookie_client, task.session or "default", parent_fid
        )
        links = await transfer.transfer_and_link(
            cookie_client,
            (task.share_id, entry.stoken),
            [leaf for _, leaf in pairs],
            temp_fid,
        )
        self._links[task.id] = {
            sub_id: link for (sub_id, _), link in zip(pairs, links)
        }

    async def _download_all(self, task: Task, flag: asyncio.Event) -> None:
        """Semaphore 限流并发下载；子项失败不中断其他子项，取消经 cancel_flag 协作传播"""
        semaphore = asyncio.Semaphore(self.settings.concurrency)
        http = httpx.AsyncClient()
        try:

            async def worker(sub: SubItem) -> None:
                if sub.status == "done":  # retry 时跳过已成功子项
                    return
                async with semaphore:
                    if flag.is_set():
                        raise asyncio.CancelledError()
                    dest = self._dest_for(sub, task)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    sub.status = "downloading"
                    self._publish_item(task, sub)
                    try:
                        await download_file(
                            http,
                            self._links[task.id][sub.id],
                            self._cookie_for(task),
                            dest,
                            on_progress=lambda done, total: self._progress(task, sub, done, total),
                            cancel_flag=flag,
                        )
                    except asyncio.CancelledError:
                        sub.status = "cancelled"
                        self._publish_item(task, sub)
                        raise
                    except Exception as exc:  # noqa: BLE001 - 单文件失败只记子项
                        sub.status = "failed"
                        sub.error = str(exc) or exc.__class__.__name__
                        self._publish_item(task, sub)
                    else:
                        sub.done_bytes = sub.size
                        sub.done_at = time.time()
                        # 到期在完成瞬间用当时的保留时长定死并持久化：
                        # 之后调改设置只对新下载的文件生效
                        sub.keep_until = sub.done_at + max(
                            1, self.settings.retention_minutes) * 60
                        sub.status = "done"
                        self._publish_item(task, sub)

            await asyncio.gather(
                *(worker(sub) for sub in task.items), return_exceptions=True
            )
            # Python 3.10：gather(return_exceptions=True) 会把子协程的
            # CancelledError 吞成结果值，这里按取消标志补抛，交给外层收尾
            if flag.is_set():
                raise asyncio.CancelledError()
        finally:
            await http.aclose()

    def _download_base(self) -> Path:
        """下载根目录（相对则相对 project_root）"""
        base = Path(self.settings.download_dir)
        if not base.is_absolute():
            base = project_root() / base
        return base

    def _dest_for(self, sub: SubItem, task: Task) -> Path:
        """落盘全路径：download_dir/{session}/{task_id}/（相对则相对 project_root）+ path 分段 sanitize

        任务目录隔离：同用户不同任务的同名文件互不覆盖、并发互不踩写，
        也不会被其他任务的取消清理误删
        """
        base = self._download_base()
        if task.session:
            base = base / task.session
        return (base / task.id).joinpath(*sanitize_rel(sub.path).split("/"))

    def _cleanup_local(self, task: Task) -> int:
        """取消收尾：删除本地未完成文件的残留（部分写入/空占位），返回删除数。

        已 done 的子项文件保留（用户可能已经用它的下载链接交付了）；随后从
        各删除点深→浅收拢空目录（任务目录、已空出的用户名目录一并移除）——
        rmdir 仅对空目录成功，非空即停（保护其他任务的文件），绝不越过
        下载根。cloud 临时由 _cleanup 负责，这里只管磁盘
        """
        base = self._download_base()
        removed = 0
        for sub in task.items:
            if sub.status == "done":
                continue
            dest = self._dest_for(sub, task)
            try:
                dest.unlink()
                removed += 1
            except OSError:
                continue  # 占用/权限：残留交给保留期清扫兜底
            parent = dest.parent
            while parent != base:
                try:
                    parent.rmdir()  # 仅空目录成功；非空即停
                except OSError:
                    break
                parent = parent.parent
        return removed

    async def _cleanup(self, task: Task) -> bool:
        """删除已转存未清理的云端临时文件；失败仅记日志，返回是否全部删除成功。

        skip_cleanup 的直链（自己的分享直连原文件）绝不删除
        """
        links = self._links.get(task.id) or {}
        fids = [link.fid for link in links.values() if link.fid and not link.skip_cleanup]
        if not fids:
            return True
        try:
            client = self._client_factory(None)
            client.cookie = self._cookie_for(task)
            ok = await transfer.delete_fids(client, fids)
        except Exception as exc:  # noqa: BLE001 - 清理失败不炸任务
            logger.warning("清理云端临时文件失败: %s", exc)
            ok = False
        finally:
            self._links.pop(task.id, None)
        return bool(ok)

    def _progress(self, task: Task, sub: SubItem, done: int, total: int) -> None:
        sub.done_bytes = done
        # 进度节流：同一子项 0.3s 内只推一次 SSE（完成时必推，终态不丢）
        key = (task.id, sub.id)
        now = time.monotonic()
        last = self._progress_ts.get(key)
        if last is not None and now - last < PROGRESS_INTERVAL_SECONDS and done < total:
            return
        self._progress_ts[key] = now
        self._publish_item(task, sub)

    def _set_status(self, task: Task, status: TaskStatus, message: str) -> None:
        task.status = status
        task.message = message
        self._publish_task(task)

    def _finish(self, task: Task, status: TaskStatus, message: str) -> None:
        task.status = status
        task.message = message
        task.terminal_at = time.time()
        self._publish_task(task)
        self._runners.pop(task.id, None)
        self._leaves.pop(task.id, None)
        for key in [k for k in self._progress_ts if k[0] == task.id]:
            self._progress_ts.pop(key, None)
        if status is not TaskStatus.failed:
            # failed 保留 Cookie 供 retry；其余终态立即清除
            self._cookies.pop(task.id, None)
        # 云端临时文件删除不压在任务终态上做，交付端也没有清理职责：
        # 文件由保留期清扫按冻结的 keep_until 到点删除

    def _prune_terminal(self) -> None:
        """修剪内存:终态超过 TTL 的 done/cancelled 任务与孤儿取消标志收出内存。

        历史已 upsert 进 tasks.json（persist 先行），UI 与下载链接走历史合并，
        不受修剪影响；failed 任务保留（retry 需要 Cookie 与待重试子项）
        """
        if not self._tasks:
            return
        now = time.time()
        for task_id in list(self._tasks):
            task = self._tasks[task_id]
            stale = (task.terminal_at is not None
                     and now - task.terminal_at > TERMINAL_TTL_SECONDS)
            if stale and task.status in (TaskStatus.done, TaskStatus.cancelled):
                self._tasks.pop(task_id, None)
                self._cancel_flags.pop(task_id, None)

    def on_settings_changed(self) -> None:
        """设置保存后的联动钩子（保留期清扫循环重启由 main 的 sweeper 负责）"""
        return None

    def _start(self, task_id: str, cookie: str = "") -> None:
        self._runners[task_id] = asyncio.create_task(self.run(task_id))
        if cookie:
            self._cookies[task_id] = cookie

    def _cookie_for(self, task: Task) -> str:
        """任务所属用户的 Cookie（仅内存）；缺失时返回空串（引擎报需要 Cookie）"""
        return self._cookies.get(task.id, "")

    def _publish_task(self, task: Task) -> None:
        self._bus.publish(
            {
                "type": "task",
                "task_id": task.id,
                "status": task.status.value,
                "message": task.message,
                "session": task.session,
            }
        )

    def _publish_item(self, task: Task, sub: SubItem) -> None:
        self._bus.publish(
            {
                "type": "item",
                "task_id": task.id,
                "item_id": sub.id,
                "done_bytes": sub.done_bytes,
                "total_bytes": sub.size,
                "status": sub.status,
                "session": task.session,
            }
        )


def _under_any(path: str, dir_paths: set[str]) -> bool:
    """path 是否落在任一已选目录之下（含多级）"""
    return any(path.startswith(d + "/") for d in dir_paths)


def _history_expired(raw: dict, now: float | None = None) -> bool:
    """tasks.json 历史条目是否已超保留期：优先按终态时刻，老数据回退 created_at"""
    if str(raw.get("status") or "") not in {s.value for s in _TERMINAL}:
        return False
    anchor = float(raw.get("terminal_at") or 0.0) or float(raw.get("created_at") or 0.0)
    return anchor + HISTORY_RETENTION_SECONDS < (now if now is not None else time.time())


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "share_id": task.share_id,
        "title": task.title,
        "created_at": task.created_at,
        "status": task.status.value,
        "message": task.message,
        "passcode": task.passcode,
        "session": task.session,
        "terminal_at": task.terminal_at,
        "items": [
            {
                "id": s.id,
                "name": s.name,
                "path": s.path,
                "size": s.size,
                "done_bytes": s.done_bytes,
                "status": s.status,
                "error": s.error,
                "done_at": s.done_at,
                "keep_until": s.keep_until,
            }
            for s in task.items
        ],
    }


def _task_from_dict(raw: dict) -> Task:
    return Task(
        id=str(raw["id"]),
        share_id=str(raw["share_id"]),
        title=str(raw.get("title") or ""),
        created_at=float(raw.get("created_at") or 0.0),
        status=TaskStatus(str(raw["status"])),
        items=[
            SubItem(
                id=str(item["id"]),
                name=str(item.get("name") or ""),
                path=str(item.get("path") or ""),
                size=int(item.get("size") or 0),
                done_bytes=int(item.get("done_bytes") or 0),
                status=str(item.get("status") or "waiting"),
                error=item.get("error"),
                done_at=float(item["done_at"]) if item.get("done_at") else None,
                keep_until=float(item["keep_until"]) if item.get("keep_until") else None,
            )
            for item in raw.get("items") or []
            if isinstance(item, dict)
        ],
        message=str(raw.get("message") or ""),
        passcode=str(raw.get("passcode") or ""),
        session=str(raw.get("session") or ""),
        terminal_at=float(raw["terminal_at"]) if raw.get("terminal_at") else None,
    )
