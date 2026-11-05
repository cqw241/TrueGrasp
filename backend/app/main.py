from fastapi import FastAPI

from app.api import health


def create_app() -> FastAPI:
    app = FastAPI(title="真懂 AI", version="0.1.0")
    app.include_router(health.router, prefix="/api")
    return app


app = create_app()
