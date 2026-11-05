from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from app.api import decision, health, stream, student, teacher
from app.config import get_settings
from app.db import get_engine, init_db
from app.knowledge.graph import NxKnowledgeGraph
from app.orchestration.bus import EventBus
from app.orchestration.runtime import OrchestrationError, Orchestrator


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
        try:
            async with Orchestrator.open(
                business_engine,
                checkpoint_path or get_settings().checkpoint_db_path,
                bus,
                **dependencies,
            ) as runtime:
                app.state.orchestrator = runtime
                app.state.event_bus = bus
                yield
        finally:
            await bus.close()
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
