"""SSE 事件流。前端只订阅这一个流，按 type 和 task_id 分发。"""

from collections.abc import AsyncIterable

from fastapi import APIRouter, Request
from fastapi.sse import EventSourceResponse

from app.contracts.events import StreamEvent

router = APIRouter(tags=["stream"])


@router.get("/events", response_class=EventSourceResponse)
async def events(request: Request) -> AsyncIterable[StreamEvent]:
    async with request.app.state.event_bus.subscribe() as subscription:
        async for event in subscription:
            yield event
