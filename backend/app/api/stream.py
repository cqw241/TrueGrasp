"""SSE 事件流。前端只订阅这一个流，按 type 和 task_id 分发。"""

import asyncio
from collections.abc import AsyncIterable
from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.sse import EventSourceResponse
from sqlmodel import Session

from app.auth import COOKIE, Principal
from app.classroom import Classroom, can_see, owned_classes
from app.contracts.entities import Evidence, Task
from app.contracts.events import StreamEvent

router = APIRouter(tags=["stream"])


def visible(event: StreamEvent, principal: Principal, engine) -> bool:
    with Session(engine) as session:
        if hasattr(event, "task_id"):
            return can_see(session, principal, session.get(Task, event.task_id))
        if event.type == "evidence_added":
            return can_see(session, principal, session.get(Evidence, event.evidence_id))
        if event.type == "mastery_changed" and principal.role == "student":
            return event.student_id == principal.student_id
        if event.type in {"clock_advanced", "mastery_changed"} and principal.role == "teacher":
            return (
                event.class_id is not None
                and session.exec(
                    owned_classes(principal.teacher_id).where(Classroom.id == event.class_id)
                ).first()
                is not None
            )
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
