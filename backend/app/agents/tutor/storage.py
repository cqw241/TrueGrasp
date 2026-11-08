"""Private tutor state; business writes and turn receipts commit together."""

from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class TutorSession(SQLModel, table=True):
    __tablename__ = "tutor_session"

    task_id: int = Field(foreign_key="task.id", primary_key=True)
    state: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class TutorReceipt(SQLModel, table=True):
    __tablename__ = "tutor_receipt"

    key: str = Field(primary_key=True)
    result: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
