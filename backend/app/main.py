import asyncio
import signal
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from types import FrameType
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from app.agents.verifier import create_verifier
from app.api import auth, decision, health, stream, student, teacher
from app.auth import AuthStore, authorize
from app.clock.service import TeachingClock
from app.config import get_settings
from app.db import get_engine, init_db
from app.knowledge.graph import NxKnowledgeGraph
from app.llm.client import OpenAICompatClient
from app.orchestration.bus import EventBus
from app.orchestration.runtime import OrchestrationError, Orchestrator


@contextmanager
def _close_stream_on_exit_signal(bus: EventBus) -> Iterator[None]:
    """收到 Ctrl+C 或 SIGTERM 时立即结束 SSE 流，再交给原来的处理函数（uvicorn 的优雅停机）。

    uvicorn 要等所有连接结束才执行 lifespan 的收尾，而 SSE 连接不会自己结束：旧进程一直挂着，
    前端连在已经停止的后端上，既收不到事件也不会重连，--reload 也会卡住。
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def handle(sig: int, frame: FrameType | None) -> None:
        asyncio.run_coroutine_threadsafe(bus.close(), loop)
        handler = previous[sig]
        if callable(handler):
            handler(sig, frame)
        else:  # SIG_DFL 或 SIG_IGN：恢复后重新触发，保持原来的行为
            signal.signal(sig, handler)
            signal.raise_signal(sig)

    for sig in previous:
        signal.signal(sig, handle)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def _follow_real_date(clock: TeachingClock, interval: float = 30) -> None:
    while True:
        await asyncio.sleep(interval)
        if clock.needs_date_sync():
            await clock.scan()


def create_app(
    *,
    engine: Engine | None = None,
    checkpoint_path: str | None = None,
    orchestration_dependencies: dict[str, Any] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        business_engine = engine if engine is not None else get_engine()
        app.state.auth_store = AuthStore(get_settings().auth_db_path)
        init_db(business_engine)
        knowledge = NxKnowledgeGraph.from_engine(business_engine)
        app.state.knowledge = knowledge
        dependencies = {"knowledge": knowledge, **(orchestration_dependencies or {})}
        bus = EventBus()
        llm = None
        try:
            settings = get_settings()
            if settings.llm_api_key and "planner" not in dependencies:
                from app.agents.planner import PlanningPolicy, create_planner

                llm = OpenAICompatClient(settings)
                dependencies["planner"] = create_planner(
                    engine=business_engine,
                    llm=llm,
                    knowledge=dependencies["knowledge"],
                    policy=PlanningPolicy(
                        class_review_min_students=settings.planner_class_review_min_students,
                        retest_delay_days=settings.planner_retest_delay_days,
                    ),
                )
            clock = TeachingClock(
                business_engine,
                dependencies["knowledge"],
                bus,
                mode=settings.teaching_clock_mode,
                initial_date=settings.teaching_clock_initial_date,
            )
            dependencies.setdefault("teaching_date", clock.current_date)
            app.state.clock = clock
            if settings.llm_api_key and "verifier" not in dependencies:
                if llm is None:
                    llm = OpenAICompatClient(settings)
                dependencies["verifier"] = create_verifier(
                    llm=llm, knowledge=dependencies["knowledge"]
                )
            if settings.llm_api_key and "tutor" not in dependencies:
                from app.agents.tutor import create_tutor

                if llm is None:
                    llm = OpenAICompatClient(settings)
                dependencies["tutor"] = create_tutor(
                    llm=llm,
                    knowledge=dependencies["knowledge"],
                    engine=business_engine,
                    teaching_date=dependencies.get("teaching_date"),
                )
            async with Orchestrator.open(
                business_engine,
                checkpoint_path or get_settings().checkpoint_db_path,
                bus,
                **dependencies,
            ) as runtime:
                app.state.orchestrator = runtime
                app.state.event_bus = bus
                clock.wake = runtime.recover_pending
                await clock.scan()
                clock_job = (
                    asyncio.create_task(_follow_real_date(clock), name="teaching-clock")
                    if clock.mode == "real"
                    else None
                )
                try:
                    with _close_stream_on_exit_signal(bus):
                        yield
                finally:
                    if clock_job is not None:
                        clock_job.cancel()
                        with suppress(asyncio.CancelledError):
                            await clock_job
        finally:
            await bus.close()
            if llm is not None:
                await llm.aclose()
            if engine is None:
                business_engine.dispose()

    app = FastAPI(title="真懂 AI", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(OrchestrationError)
    async def orchestration_error(_: Request, exc: OrchestrationError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    for module in (health, auth):
        app.include_router(module.router, prefix="/api")
    for module in (teacher, student, decision, stream):
        app.include_router(module.router, prefix="/api", dependencies=[Depends(authorize)])
    return app


app = create_app()
