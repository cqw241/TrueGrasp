import asyncio
import signal
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from types import FrameType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from app.agents.verifier import create_verifier
from app.api import decision, health, stream, student, teacher
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


def create_app(
    *,
    engine: Engine | None = None,
    checkpoint_path: str | None = None,
    orchestration_dependencies: dict[str, Any] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        business_engine = engine if engine is not None else get_engine()
        init_db(business_engine)
        knowledge = NxKnowledgeGraph.from_engine(business_engine)
        app.state.knowledge = knowledge
        dependencies = {"knowledge": knowledge, **(orchestration_dependencies or {})}
        bus = EventBus()
        llm = None
        try:
            settings = get_settings()
            if settings.llm_api_key and "verifier" not in dependencies:
                llm = OpenAICompatClient(settings)
                dependencies["verifier"] = create_verifier(
                    llm=llm, knowledge=dependencies["knowledge"]
                )
            async with Orchestrator.open(
                business_engine,
                checkpoint_path or get_settings().checkpoint_db_path,
                bus,
                **dependencies,
            ) as runtime:
                app.state.orchestrator = runtime
                app.state.event_bus = bus
                with _close_stream_on_exit_signal(bus):
                    yield
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

    for module in (health, teacher, student, decision, stream):
        app.include_router(module.router, prefix="/api")
    return app


app = create_app()
