"""SSE 事件流。前端只订阅这一个流，按 type 和 task_id 分发。"""

import asyncio
from collections.abc import AsyncIterable
from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.sse import EventSourceResponse
from sqlmodel import Session

from app.auth import COOKIE, Principal
from app.contracts.entities import Task
from app.contracts.events import StreamEvent

router = APIRouter(tags=["stream"])


def visible(event: StreamEvent, principal: Principal, engine) -> bool:
    if principal.role == "teacher":
        return True
    if event.type in {"token", "message_done"}:
        with Session(engine) as session:
            task = session.get(Task, event.task_id)
            return task is not None and task.student_id == principal.student_id
    if event.type in {"hypothesis_updated", "evidence_added", "mastery_changed", "task_changed"}:
        return event.student_id is not None and event.student_id == principal.student_id
    return False


@router.get("/events", response_class=EventSourceResponse)
async def events(request: Request) -> AsyncIterable[StreamEvent]:
    token = request.cookies.get(COOKIE)
    principal = request.state.principal
    async with request.app.state.event_bus.subscribe() as subscription:
        pending = asyncio.create_task(anext(subscription))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=1)
                if request.app.state.auth_store.principal(token) != principal:
                    break
                if not done:
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    break
                pending = asyncio.create_task(anext(subscription))
                if visible(event, principal, request.app.state.orchestrator.engine):
                    yield event
        finally:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
