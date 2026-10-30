"""下载任务引擎：任务状态机、并发下载、取消清理、重试、启动清扫与历史持久化"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import Settings, project_root
from app.engine.disk import disk_usage_bytes, snapshot_from_usage
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
# UI 与下载链接仍能经 tasks.json 历史寻址（tasks_for 会合并历史）。
# failed 另给 24h，供用户重试；到期后连同 Cookie/待重试项与 .part 一并清理
TERMINAL_TTL_SECONDS = 1800
FAILED_TTL_SECONDS = 24 * 3600
# 终态任务的 tasks.json 历史保留期：更老的条目在 persist 落盘/载入时修剪，
# 防 tasks.json 随时间无限增长；带未来 keep_until 的条目会保留到文件到期
HISTORY_RETENTION_SECONDS = 7 * 24 * 3600
# 进度事件节流间隔：SSE 全量转发会让总线/前端被高频事件淹没，
# 每 (task, 子项) 至多 ~1s 推一次（完成必推）
PROGRESS_INTERVAL_SECONDS = 1.0
# 排队任务磁盘复查间隔：即使未收到监控唤醒，也定期自查，避免永久挂起
DISK_RECHECK_SECONDS = 10.0
# 磁盘监控轮询间隔：空间释放后及时叫醒排队任务
DISK_MONITOR_INTERVAL_SECONDS = 15.0


class TaskStatus(str, Enum):
    queued = "queued"
    transferring = "transferring"
    downloading = "downloading"
    cleaning = "cleaning"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


_TERMINAL = {TaskStatus.done, TaskStatus.failed, TaskStatus.cancelled}
_TERMINAL_VALUES = frozenset(s.value for s in _TERMINAL)


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
    # 是否因磁盘空间不足排队（前端据此显示警告符号与警示色）
    disk_low: bool = False


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
        # task_id -> 用户 Cookie（仅内存；任务终态即清，避免进持久化）
        # failed 任务保留 Cookie 供 retry 使用，过期修剪时一并清除
        self._cookies: dict[str, str] = {}
        # (task_id, sub_id) -> 上次进度事件 time.monotonic()：SSE 进度节流
        self._progress_ts: dict[tuple[str, str], float] = {}
        # 任务级全局并发闸：同时在跑（转存/下载/清理）的任务数受「并发数」限制，
        # 超出的任务留在 queued 态排队等候。Condition 自带独立锁，wait 期间自动放行
        self._gate: asyncio.Condition = asyncio.Condition()
        self._active_downloads = 0
        # 已准入任务的预估待写入字节：并发任务同时下载时仍为磁盘保留余量
        self._reserved_bytes = 0
        # tasks.json 解析缓存：(mtime_ns, size, raw dicts)。历史文件只在
        # persist/freeze 时变化，热路径（/api/tasks、文件下载校验、清扫）
        # 不必反复读盘解析；raw 仅引擎内部使用，对外始终返回新 Task 对象
        self._history_cache: tuple[int, int, list[dict]] | None = None
        # 写盘串行化：persist 可能在线程池执行，退出时又会在事件循环补写一次，
        # 锁保证两次写不会交错覆盖
        self._persist_lock = threading.Lock()
        # 创建路径上的修剪节流：后台维护循环已定期修剪，避免每建一个任务
        # 都全表扫描（failed 任务现在会保留 24h，表可能较大）
        self._last_prune = 0.0

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

        if time.monotonic() - self._last_prune >= 60:
            self._prune_terminal()  # 借新任务入场顺带修剪；后台循环另会定期修剪

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
        client: Any = None
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
        finally:
            await self._aclose_client(client)

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
                terminal_at=t.terminal_at,
                disk_low=t.disk_low,
            )
            for t in self._tasks.values()
            if session is None or t.session == session
        ]

    def tasks_for(self, session: str) -> list[Task]:
        """本会话的全部任务：内存活任务 + 历史合并（去重，活的优先）"""
        live = {t.id: t for t in self.snapshot(session)}
        merged: list[Task] = list(live.values())
        for h in self.load_history(session):
            if h.id not in live:
                merged.append(h)
        return merged

    # ---- 运行 / 取消 / 重试 ------------------------------------------------

    async def run(self, task_id: str) -> None:
        """单任务主流程：queued 排队 → transferring → downloading → cleaning → 终态

        开跑前先过任务级闸门：并发已满或磁盘可用空间不足时保持 queued 等待，
        磁盘准入按本任务待写入字节预留，避免并发任务同时挤爆磁盘
        """
        task = self._tasks.get(task_id)
        if task is None or task.status is not TaskStatus.queued:
            return

        flag = self._cancel_flags[task_id]
        acquired = False
        reserved = 0
        try:
            try:
                reserved = self._pending_bytes(task)
                async with self._gate:
                    while not flag.is_set():
                        if self._active_downloads >= max(1, self.settings.concurrency):
                            self._set_wait_message(
                                task, "正在排队等候（下载并发已满）…", False
                            )
                        elif not self._disk_allows(reserved):
                            self._set_wait_message(
                                task, self._disk_wait_message(reserved), True
                            )
                        else:
                            self._active_downloads += 1
                            self._reserved_bytes += reserved
                            acquired = True
                            break
                        await self._wait_gate()
                    if flag.is_set():
                        raise asyncio.CancelledError()

                task.disk_low = False
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
            finally:
                if acquired:
                    # 释放槽位/磁盘预留并叫醒排队中的 runner 重新抢（持锁期间改计数，无竞态）
                    async with self._gate:
                        self._active_downloads -= 1
                        self._reserved_bytes = max(0, self._reserved_bytes - reserved)
                        self._gate.notify_all()
        except asyncio.CancelledError:
            if not acquired and (task.status in _TERMINAL or flag.is_set()):
                # 等待闸位期被取消：cancel() 的排队分支负责收尾，不重复清理；
                # 已获槽位的取消仍走下面统一收尾（waiting/download 子项清状态）
                return
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
            # 立即叫醒卡在并发闸上的 runner：让它复查取消标志退出，
            # 别一直挂到下一轮槽位释放才醒
            async with self._gate:
                self._gate.notify_all()

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
        try:
            client.cookie = self._cookie_for(task)
            entry = await self._share_session.resolve(task.share_id, task.passcode)
            tree = await walk_subtree(client, task.share_id, entry.stoken, "0")
            by_path = {str(e["path"]): e for e in tree if not e["dir"]}
            pairs = [(s.id, by_path[s.path]) for s in retry_subs if s.path in by_path]
        finally:
            await self._aclose_client(client)
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
        try:
            temp_fid = await transfer.ensure_temp_dir(client, self.settings.temp_dir_name)
            snapshot = await transfer.temp_snapshot(client, temp_fid)
            fids = [fid for fid in snapshot.values() if fid]
            if fids:
                await transfer.delete_fids(client, fids)
        finally:
            await self._aclose_client(client)
        return len(fids)

    def sweep_orphan_parts(self) -> int:
        """启动时清理所有 .part：任务队列不跨重启恢复，断点文件已无续传可能。

        返回删除的文件数；顺带收拢清空后的任务/会话目录
        """
        base = self._download_base()
        if not base.exists():
            return 0
        removed = 0
        parents: set[Path] = set()
        for part in base.rglob("*.part"):
            try:
                if part.is_file():
                    part.unlink()
                    removed += 1
                    parents.add(part.parent)
            except OSError:
                continue
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            while parent != base:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return removed

    # ---- 持久化 ------------------------------------------------------------

    def _history_raw(self) -> list[dict]:
        """读取 tasks.json 的原始条目（按 mtime/size 缓存，避免热路径反复解析）。

        返回缓存内部列表：仅供 persist/load_history/freeze 使用，不对外暴露
        """
        try:
            stat = self._db_path.stat()
        except OSError:
            self._history_cache = None
            return []
        key = (stat.st_mtime_ns, stat.st_size)
        cache = self._history_cache
        if cache is not None and cache[0] == key[0] and cache[1] == key[1]:
            return cache[2]
        try:
            data = json.loads(self._db_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        raws = data.get("tasks") if isinstance(data, dict) else None
        entries = [raw for raw in raws or [] if isinstance(raw, dict)]
        self._history_cache = (key[0], key[1], entries)
        return entries

    def _write_history(self, entries: list[dict]) -> None:
        """原子写回 tasks.json：紧凑 JSON + 同目录临时文件替换，避免半截文件"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._db_path.with_name(f".{self._db_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(
                json.dumps({"tasks": entries}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self._db_path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        self._history_cache = None

    def terminal_payload(self) -> list[dict]:
        """在事件循环线程内提取终态任务快照。

        写盘放线程执行时，绝不能在后台线程遍历 _tasks——下载/新建任务会同时
        改字典，触发 "dictionary changed size during iteration"
        """
        return [_task_to_dict(t) for t in self._tasks.values() if t.status in _TERMINAL]

    def persist(self, terminal: list[dict] | None = None) -> None:
        """把终态任务 upsert 进 db_path（按 id 合并既有历史，重启后不丢旧记录）

        落盘同时修剪过期历史；terminal 由调用方在事件循环线程内先取好，
        后台线程只做磁盘 I/O
        """
        if terminal is None:
            terminal = self.terminal_payload()
        with self._persist_lock:
            existing: dict[str, dict] = {}
            for raw in self._history_raw():
                if raw.get("id") and not _history_expired(raw):
                    existing[str(raw["id"])] = raw
            for raw in terminal:
                existing[str(raw["id"])] = raw
            self._write_history(list(existing.values()))

    def load_history(self, session: str | None = None) -> list[Task]:
        """从 db_path 读历史任务；过期/损坏条目跳过，可按会话过滤后解析。

        返回全新 Task 对象：调用方修改不会污染缓存
        """
        tasks: list[Task] = []
        now = time.time()
        for raw in self._history_raw():
            if session is not None and str(raw.get("session") or "") != session:
                continue
            if _history_expired(raw, now):
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
        data = {"tasks": self._history_raw()}
        if not data["tasks"]:
            return 0
        window = max(1, self.settings.retention_minutes) * 60
        changed = 0
        for raw in data["tasks"]:
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
            self._write_history(data["tasks"])
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

    @staticmethod
    async def _aclose_client(client: Any) -> None:
        """关闭临时夸克客户端；测试 fake 没有 aclose 时静默跳过"""
        close = getattr(client, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响任务结果
            logger.debug("关闭临时客户端失败: %s", exc)

    async def _transfer(self, task: Task, flag: asyncio.Event) -> None:
        """转存并取直链；LinkItem 按 (sub_id, leaf) 顺序映射回 SubItem"""
        self._set_status(task, TaskStatus.transferring, "正在转存…")
        pairs = self._leaves.get(task.id)
        if not pairs:
            raise QuarkApiError(0, "缺少转存清单")

        cookie_client = self._client_factory(None)
        try:
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
        finally:
            await self._aclose_client(cookie_client)
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

    # ---- 磁盘守门 ----------------------------------------------------------

    def disk_status(self) -> dict:
        """磁盘用量快照（管理员页展示与准入判断共用）"""
        return snapshot_from_usage(
            *disk_usage_bytes(self._download_base()),
            reserve_mb=int(self.settings.reserve_disk_mb or 0),
            inflight_bytes=self._reserved_bytes,
        )

    async def notify_disk_changed(self) -> None:
        """磁盘空间或预留设置变化时，叫醒排队任务重新评估"""
        async with self._gate:
            self._gate.notify_all()

    def _pending_bytes(self, task: Task) -> int:
        """本任务待写入磁盘的预估字节：扣除已 done 子项与已有 .part 续传部分"""
        total = 0
        for sub in task.items:
            if sub.status == "done":
                continue
            remaining = max(0, int(sub.size or 0))
            try:
                dest = self._dest_for(sub, task)
                part = dest.with_name(dest.name + ".part")
                if part.is_file():
                    remaining = max(0, remaining - part.stat().st_size)
            except OSError:
                pass
            total += remaining
        return total

    def _disk_allows(self, required: int) -> bool:
        """可用空间（扣除在途预留）减去管理员预留值后，是否容纳本任务"""
        try:
            status = self.disk_status()
        except OSError:
            return True  # 统计失败不阻断下载
        return status["available_bytes"] - status["reserve_bytes"] >= required

    def _disk_wait_message(self, required: int) -> str:
        """排队提示只说明状态与所需空间，其余明细留给管理员面板展示"""
        return f"磁盘空间不足，本任务需 {_fmt_bytes(required)}；已排队等待空间释放…"

    def _set_wait_message(self, task: Task, message: str, disk_low: bool) -> None:
        """排队提示去重：避免监控轮询时重复推送同一状态"""
        if task.message == message and task.disk_low == disk_low:
            return
        task.message = message
        task.disk_low = disk_low
        self._publish_task(task)

    async def _wait_gate(self) -> None:
        """等待闸门变化；超时后自查一次，监控失效也不会永久排队"""
        try:
            await asyncio.wait_for(self._gate.wait(), timeout=DISK_RECHECK_SECONDS)
        except asyncio.TimeoutError:
            pass

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
        rmdir 仅对空目录成功，非空即停（保护其他任务的文件），避免越过
        下载根。cloud 临时由 _cleanup 负责，这里只管磁盘
        """
        base = self._download_base()
        removed = 0
        for sub in task.items:
            if sub.status == "done":
                continue
            dest = self._dest_for(sub, task)
            # 半成品 .part 与空占位都要删；只删 dest 会让断点残留长期占盘
            for path in (dest, dest.with_name(dest.name + ".part")):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue  # 不存在/占用/权限：残留交给保留期清扫兜底
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

        skip_cleanup 的直链（自己的分享直连原文件）不删除
        """
        links = self._links.get(task.id) or {}
        fids = [link.fid for link in links.values() if link.fid and not link.skip_cleanup]
        if not fids:
            return True
        try:
            client = self._client_factory(None)
            try:
                client.cookie = self._cookie_for(task)
                ok = await transfer.delete_fids(client, fids)
            finally:
                await self._aclose_client(client)
        except Exception as exc:  # noqa: BLE001 - 清理失败不炸任务
            logger.warning("清理云端临时文件失败: %s", exc)
            ok = False
        finally:
            self._links.pop(task.id, None)
        return bool(ok)

    def _progress(self, task: Task, sub: SubItem, done: int, total: int) -> None:
        sub.done_bytes = done
        # 进度节流：同一子项在 PROGRESS_INTERVAL_SECONDS 内只推一次 SSE（完成必推）
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
        task.disk_low = False
        self._publish_task(task)

    def _finish(self, task: Task, status: TaskStatus, message: str) -> None:
        task.status = status
        task.message = message
        task.disk_low = False
        task.terminal_at = time.time()
        self._publish_task(task)
        self._runners.pop(task.id, None)
        self._leaves.pop(task.id, None)
        for key in [k for k in self._progress_ts if k[0] == task.id]:
            self._progress_ts.pop(key, None)
        if status is not TaskStatus.failed:
            # failed 保留 Cookie/待重试项供 retry；其余终态立即清除
            self._cookies.pop(task.id, None)
            self._pending.pop(task.id, None)
        # 云端临时文件删除不压在任务终态上做，交付端也没有清理职责：
        # 文件由保留期清扫按冻结的 keep_until 到点删除

    def _prune_terminal(self) -> None:
        """修剪内存里的过期终态任务，释放字典与 Cookie 引用。

        done/cancelled 超过 TERMINAL_TTL_SECONDS 即移除；failed 保留
        FAILED_TTL_SECONDS 供重试，到期后连同 .part 残留一起清理。历史
        已 upsert 进 tasks.json，UI 与下载链接走历史合并，不受修剪影响
        """
        if not self._tasks:
            self._last_prune = time.monotonic()
            return
        self._last_prune = time.monotonic()
        now = time.time()
        for task_id in list(self._tasks):
            task = self._tasks[task_id]
            if task.terminal_at is None:
                continue
            age = now - task.terminal_at
            if task.status in (TaskStatus.done, TaskStatus.cancelled):
                stale = age > TERMINAL_TTL_SECONDS
            elif task.status is TaskStatus.failed:
                stale = age > FAILED_TTL_SECONDS
                if stale:
                    self._cleanup_local(task)  # 失败的半成品不再有重试价值
            else:
                continue
            if not stale:
                continue
            self._drop_task(task_id)

    def _drop_task(self, task_id: str) -> None:
        """移除任务的全部内存引用（含 Cookie/待重试项/进度节流表）"""
        self._tasks.pop(task_id, None)
        self._cancel_flags.pop(task_id, None)
        self._runners.pop(task_id, None)
        self._preparers.pop(task_id, None)
        self._pending.pop(task_id, None)
        self._leaves.pop(task_id, None)
        self._links.pop(task_id, None)
        self._cookies.pop(task_id, None)
        for key in [k for k in self._progress_ts if k[0] == task_id]:
            self._progress_ts.pop(key, None)

    def maintenance(self) -> None:
        """周期维护入口：修剪过期终态任务（由后台监控循环低频调用）"""
        self._prune_terminal()

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
                "disk_low": task.disk_low,
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
    """path 是否落在任一已选目录之下（逐级向上匹配，O(深度)）"""
    if not dir_paths:
        return False
    parent = path
    while "/" in parent:
        parent = parent.rsplit("/", 1)[0]
        if parent in dir_paths:
            return True
    return False


def _fmt_bytes(n: int) -> str:
    """磁盘提示用的人类可读大小"""
    n = max(0, int(n or 0))
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{n / 1024 ** 2:.0f} MB"


def _history_expired(raw: dict, now: float | None = None) -> bool:
    """tasks.json 历史条目是否已超保留期：优先按终态时刻，老数据回退 created_at。

    关联文件到期后历史还要再留一个保留期：清扫循环有间隔、服务也可能停机，
    若历史与 keep_until 同时过期，文件会在清扫前失去记录而永久残留
    """
    if str(raw.get("status") or "") not in _TERMINAL_VALUES:
        return False
    now = now if now is not None else time.time()
    try:
        anchor = float(raw.get("terminal_at") or 0.0) or float(raw.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return True  # 损坏条目不参与历史合并
    if anchor + HISTORY_RETENTION_SECONDS >= now:
        return False
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        keep_until = item.get("keep_until")
        if isinstance(keep_until, (int, float)):
            if keep_until + HISTORY_RETENTION_SECONDS > now:
                return False
    return True


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
        "disk_low": task.disk_low,
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
        disk_low=bool(raw.get("disk_low")),
    )
