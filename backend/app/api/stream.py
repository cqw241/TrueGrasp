"""SSE 事件流。前端只订阅这一个流，按 type 和 task_id 分发。"""

from collections.abc import AsyncIterable

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse

from app.api.stub import not_implemented
from app.contracts.events import StreamEvent

router = APIRouter(tags=["stream"])


def _stub() -> None:
    not_implemented("T03")


# 流开始后不能再返回错误码，桩放在依赖里，在开始推送前返回 501；实现时去掉这个依赖。
@router.get("/events", response_class=EventSourceResponse, dependencies=[Depends(_stub)])
async def events() -> AsyncIterable[StreamEvent]:
    return
    yield
