"""single-flight 广播 + 并发准入。

Broadcaster 把同一路对 YouTube 的拉取字节，公平地分发给所有当前订阅者：
每个订阅者独立的有界队列，publish 用 put_nowait 同时投递给所有人，
一个订阅者太慢导致队列满时，只丢弃/断开那一个订阅者，绝不阻塞整个
广播循环（也就不会拖慢/克扣其他订阅者应得的那一份带宽）。
"""

import asyncio


class Broadcaster:
    def __init__(self, queue_size):
        self._queue_size = queue_size
        self._subscribers = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._error = None
        self._buffer = []

    async def subscribe(self):
        """新订阅者先原样收到目前为止已发布的全部字节（含 fmp4 的 ftyp/moov 初始化段），
        再接上后续实时字节——否则中途加入的订阅者会拿到一段没有容器头的流，播放器
        收到字节却无法解码任何画面。用统一的锁把“追加到 buffer + 拍下订阅者快照”
        和“回放 buffer + 注册订阅者”绑成两段原子操作，保证不丢不重。
        """
        async with self._lock:
            sid = self._next_id
            self._next_id += 1
            queue = asyncio.Queue()  # 无界；慢订阅者改由 publish() 里的 qsize 阈值判断丢弃
            for chunk in self._buffer:
                queue.put_nowait(chunk)
            self._subscribers[sid] = queue
        return sid, queue

    async def unsubscribe(self, sid):
        async with self._lock:
            self._subscribers.pop(sid, None)

    async def subscriber_count(self):
        async with self._lock:
            return len(self._subscribers)

    async def publish(self, chunk):
        async with self._lock:
            self._buffer.append(chunk)
            subs = list(self._subscribers.items())
        for sid, queue in subs:
            if queue.qsize() >= self._queue_size:
                await self._drop(sid, queue)
            else:
                queue.put_nowait(chunk)

    async def _drop(self, sid, queue):
        async with self._lock:
            self._subscribers.pop(sid, None)
        queue.put_nowait(None)

    async def finish(self, error=None):
        self._error = error
        async with self._lock:
            subs = list(self._subscribers.values())
            self._subscribers.clear()
            self._buffer = []
        for queue in subs:
            await queue.put(None)

    @property
    def error(self):
        return self._error


class Admission:
    """distinct video_id 的并发准入信号量；同一 video_id 的 single-flight 加入不占名额。"""

    def __init__(self, limit):
        self._limit = limit
        self._current = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self):
        async with self._lock:
            if self._current >= self._limit:
                return False
            self._current += 1
            return True

    async def release(self):
        async with self._lock:
            self._current = max(0, self._current - 1)


class StreamRegistry:
    """video_id -> Broadcaster 的 single-flight 注册表。"""

    def __init__(self, queue_size):
        self._queue_size = queue_size
        self._streams = {}
        self._lock = asyncio.Lock()

    async def join_or_create(self, video_id):
        async with self._lock:
            existing = self._streams.get(video_id)
            if existing is not None:
                return existing, False
            broadcaster = Broadcaster(self._queue_size)
            self._streams[video_id] = broadcaster
            return broadcaster, True

    async def remove(self, video_id):
        async with self._lock:
            self._streams.pop(video_id, None)
