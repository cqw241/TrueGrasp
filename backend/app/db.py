"""业务库连接。LangGraph checkpoint 另存一个库，不在这里。"""

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, event, inspect, text
from sqlmodel import Session, SQLModel, create_engine

import app.classroom  # noqa: F401
import app.clock.storage  # noqa: F401  注册时钟私有表
import app.contracts.entities  # noqa: F401  注册全部表
import app.homework.storage  # noqa: F401  注册作业草稿与原文件表
import app.orchestration.storage  # noqa: F401
from app.config import get_settings


def _enable_foreign_keys(dbapi_conn, _record) -> None:  # noqa: ANN001
    dbapi_conn.execute("PRAGMA foreign_keys=ON")


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _enable_foreign_keys)
    return engine


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def init_db(engine: Engine | None = None) -> None:
    from app.contracts.entities import KnowledgePoint
    from app.contracts.values import (
        PENDING_KNOWLEDGE_POINT_ID,
        PENDING_KNOWLEDGE_POINT_NAME,
    )
    from app.ledger.service import protect_evidence

    business_engine = engine or get_engine()
    SQLModel.metadata.create_all(business_engine)
    # Existing SQLite files predate class ownership. NULL is intentionally unassigned.
    # Adding a nullable column leaves the append-only evidence trigger and row IDs intact.
    with business_engine.begin() as connection:
        connection.execute(text("BEGIN IMMEDIATE"))
        for table in (
            "teaching_goal",
            "misconception",
            "item",
            "evidence",
            "task",
            "event",
            "question_photo",
        ):
            if "class_id" not in {
                column["name"] for column in inspect(connection).get_columns(table)
            }:
                connection.execute(
                    text(
                        f"ALTER TABLE {table} ADD COLUMN class_id VARCHAR REFERENCES classroom(id)"
                    )
                )
                connection.execute(text(f"CREATE INDEX ix_{table}_class_id ON {table}(class_id)"))
    with Session(business_engine) as session:
        if session.get(KnowledgePoint, PENDING_KNOWLEDGE_POINT_ID) is None:
            session.add(
                KnowledgePoint(
                    id=PENDING_KNOWLEDGE_POINT_ID,
                    lecture=0,
                    section=0,
                    name=PENDING_KNOWLEDGE_POINT_NAME,
                )
            )
            session.commit()
    protect_evidence(business_engine)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：``session: Annotated[Session, Depends(get_session)]``。"""
    with Session(get_engine()) as session:
        yield session
