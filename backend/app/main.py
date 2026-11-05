from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import decision, health, stream, student, teacher
from app.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="真懂 AI", version="0.1.0", lifespan=lifespan)
    for module in (health, teacher, student, decision, stream):
        app.include_router(module.router, prefix="/api")
    return app


app = create_app()
