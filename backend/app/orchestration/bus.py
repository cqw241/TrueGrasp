"""单进程 SSE 广播。订阅只接收连接期间的事件，详情由 REST 重读。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.contracts.events import StreamEvent


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[StreamEvent | None]] = set()
        self._closed = False

    async def publish(self, event: StreamEvent) -> None:
        for queue in self._subscribers:
            queue.put_nowait(event.model_copy(deep=True))

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[StreamEvent]]:
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        if self._closed:
            queue.put_nowait(None)
        else:
            self._subscribers.add(queue)

        async def iterate() -> AsyncIterator[StreamEvent]:
            while (event := await queue.get()) is not None:
                yield event

        try:
            yield iterate()
        finally:
            self._subscribers.discard(queue)

    async def close(self) -> None:
        self._closed = True
        for queue in self._subscribers:
            queue.put_nowait(None)
        self._subscribers.clear()
