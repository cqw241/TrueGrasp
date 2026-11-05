"""编排私有持久化：节点结果、待调度工作和按学生记录的已展示题目。

与对应业务变更使用同一事务；不属于 T02 的实体或对外契约。
"""

from typing import Any

from sqlalchemy import JSON, Column, Engine
from sqlmodel import Field, Session, SQLModel

from app.contracts.entities import Item
from app.contracts.enums import ItemPurpose


class NodeReceipt(SQLModel, table=True):
    __tablename__ = "orchestration_receipt"

    key: str = Field(primary_key=True)
    result: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class PendingWork(SQLModel, table=True):
    __tablename__ = "orchestration_work"

    key: str = Field(primary_key=True)  # event:<id> 或 task:<id>
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    pending: bool = Field(default=True, index=True)


class ItemExposure(SQLModel, table=True):
    __tablename__ = "orchestration_item_exposure"

    student_id: str = Field(foreign_key="student.id", primary_key=True)
    fingerprint: str = Field(primary_key=True)
    task_id: int = Field(foreign_key="task.id")
    item_id: str = Field(foreign_key="item.id")


def init_storage(engine: Engine) -> None:
    SQLModel.metadata.create_all(
        engine,
        tables=[NodeReceipt.__table__, PendingWork.__table__, ItemExposure.__table__],
    )


def queue_work(
    session: Session, key: str, payload: dict[str, Any], *, reactivate: bool = False
) -> None:
    work = session.get(PendingWork, key)
    if work is None:
        session.add(PendingWork(key=key, payload=payload))
    elif reactivate:
        work.pending = True
        work.payload = payload


def record_exposure(session: Session, student_id: str, task_id: int, item: Item) -> None:
    if item.fingerprint is None:
        return
    exposure = session.get(ItemExposure, (student_id, item.fingerprint))
    if exposure is None:
        session.add(
            ItemExposure(
                student_id=student_id,
                task_id=task_id,
                item_id=item.id,
                fingerprint=item.fingerprint,
            )
        )
    elif item.purpose == ItemPurpose.VERIFICATION and (
        exposure.task_id != task_id or exposure.item_id != item.id
    ):
        raise ValueError("验证题的指纹与该学生已展示的题目重复")
