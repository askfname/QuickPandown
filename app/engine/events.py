"""事件总线：任务/子项状态变更广播给订阅者，每个订阅者一个 asyncio.Queue"""

from __future__ import annotations

import asyncio

# 单订阅者队列上限：进度事件可能高频连发，慢消费者（断网页面、暂停的标签页）
# 不设界会无限堆积内存。满时丢最旧（进度流允许丢帧，终态由 refreshTasks 兜底）
MAX_QUEUE_SIZE = 1024


class EventBus:
    """进程内发布/订阅；publish 同步投递（put_nowait），不阻塞调用方

    subscribe 可声明目标会话：只投递该会话的事件，避免每个 SSE 连接都
    为其他用户的事件入队/出队。session=None 表示全量订阅（持久化循环）
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._sessions: dict[int, str | None] = {}

    def subscribe(self, session: str | None = None) -> asyncio.Queue:
        """注册一个订阅者并返回其事件队列（有界，防慢消费者堆积）"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._subscribers.append(queue)
        self._sessions[id(queue)] = session
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """移除订阅者；队列不在册时静默忽略"""
        self._sessions.pop(id(queue), None)
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def publish(self, event: dict) -> None:
        """向匹配会话的订阅者投递事件（各队列独立持有引用；满则丢最旧）"""
        event_session = event.get("session")
        for queue in list(self._subscribers):
            target = self._sessions.get(id(queue))
            if target is not None and event_session is not None and target != event_session:
                continue
            if queue.full():
                try:
                    queue.get_nowait()  # 丢最旧事件，保住最新进度
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
