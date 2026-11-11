"""编排私有持久化：节点结果、待调度工作和按学生记录的已展示题目。

与对应业务变更使用同一事务；不属于 T02 的实体或对外契约。
"""

from datetime import date
from typing import Any

from sqlalchemy import JSON, Column, Engine
from sqlmodel import Field, Session, SQLModel, select

from app.contracts.entities import Item, Task
from app.contracts.enums import ItemPurpose, TaskStatus, TaskType

# Persisted in Task.reason; keep this marker stable for existing backtrack tasks.
BACKTRACK_REASON_PREFIX = "先修回溯："
QUESTION_FOLLOWUP = "主动提问后的同类题："


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


class PlanningTaskContext(SQLModel, table=True):
    """Planner-private origin and cross-chapter context, committed with its Task."""

    __tablename__ = "planning_task_context"

    task_id: int = Field(primary_key=True, foreign_key="task.id")
    root_knowledge_point_id: str = Field(foreign_key="knowledge_point.id")
    context_knowledge_point_id: str | None = Field(default=None, foreign_key="knowledge_point.id")
    issue_misconception_id: str | None = None


class TeacherTaskControl(SQLModel, table=True):
    """Durable teacher control separate from the student's conversation checkpoint."""

    __tablename__ = "teacher_task_control"

    task_id: int = Field(primary_key=True, foreign_key="task.id")
    taken_over: bool = False
    overridden: bool = False
    note: str = ""


class ClassTaskOverride(SQLModel, table=True):
    """Append-only record for a class task, which has no student for an Evidence row."""

    __tablename__ = "class_task_override"

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    note: str
    action: str
    teaching_date: date


def teacher_has_student(session: Session, student_id: str | None) -> bool:
    if student_id is None:
        return False
    return (
        session.exec(
            select(TeacherTaskControl.task_id)
            .join(Task, TeacherTaskControl.task_id == Task.id)
            .where(Task.student_id == student_id, TeacherTaskControl.taken_over)
        ).first()
        is not None
    )


def matches_teacher_override(session: Session, old: Task, planned, context: dict | None) -> bool:
    if old.status == TaskStatus.DONE or (
        old.type,
        old.student_id,
        old.knowledge_point_id,
        old.round,
    ) != (planned.type, planned.student_id, planned.knowledge_point_id, planned.round):
        return False
    if old.type not in (TaskType.CLASS_REVIEW, TaskType.PRECHECK):
        if old.reason.startswith(QUESTION_FOLLOWUP) != planned.reason.startswith(QUESTION_FOLLOWUP):
            return False
        # The override covers the evidence behind this task. A new observation can
        # justify new work, even when the student, point and round are unchanged.
        return set(planned.evidence_ids) <= set(old.evidence_ids)
    if context is None:
        return True
    prior = session.get(PlanningTaskContext, old.id)
    if prior is None:
        return True
    if old.type == TaskType.CLASS_REVIEW:
        return prior.issue_misconception_id == context["issue_misconception_id"]
    return prior.context_knowledge_point_id in (None, context["context_knowledge_point_id"])


def init_storage(engine: Engine) -> None:
    SQLModel.metadata.create_all(
        engine,
        tables=[
            NodeReceipt.__table__,
            PendingWork.__table__,
            ItemExposure.__table__,
            PlanningTaskContext.__table__,
            TeacherTaskControl.__table__,
            ClassTaskOverride.__table__,
        ],
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
