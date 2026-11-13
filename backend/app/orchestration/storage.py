"""编排私有持久化：节点结果、待调度工作和按学生记录的已展示题目。

与对应业务变更使用同一事务；不属于 T02 的实体或对外契约。
"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column, Engine
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Field, Session, SQLModel, select

from app.contracts.entities import Item, Task
from app.contracts.enums import ItemPurpose, TaskStatus, TaskType

# Persisted in Task.reason; keep this marker stable for existing backtrack tasks.
BACKTRACK_REASON_PREFIX = "先修回溯："
QUESTION_FOLLOWUP = "主动提问后的同类题："


class VerificationExposureConflict(ValueError):
    """Another task already showed this verification item or its equivalent."""


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


class QuestionPhoto(SQLModel, table=True):
    """原始题目图片，只供该学生及其题目会话的模型调用读取。"""

    __tablename__ = "question_photo"

    id: str = Field(primary_key=True)
    student_id: str = Field(foreign_key="student.id", index=True)
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    data_url: str
    class_id: str | None = Field(default=None, foreign_key="classroom.id", index=True)
    created_on: date
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ClassTaskOverride(SQLModel, table=True):
    """Append-only record for a class task, which has no student for an Evidence row."""

    __tablename__ = "class_task_override"

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    note: str
    action: str
    teaching_date: date


def teacher_has_student(
    session: Session, student_id: str | None, class_id: str | None = None
) -> bool:
    if student_id is None:
        return False
    return (
        session.exec(
            select(TeacherTaskControl.task_id)
            .join(Task, TeacherTaskControl.task_id == Task.id)
            .where(
                Task.student_id == student_id,
                Task.class_id == class_id,
                TeacherTaskControl.taken_over,
            )
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
            QuestionPhoto.__table__,
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
    task = session.get(Task, task_id)
    fingerprint = f"{task.class_id}:{item.fingerprint}" if task.class_id else item.fingerprint
    result = session.execute(
        insert(ItemExposure)
        .values(
            student_id=student_id,
            task_id=task_id,
            item_id=item.id,
            fingerprint=fingerprint,
        )
        .on_conflict_do_nothing(index_elements=["student_id", "fingerprint"])
    )
    if result.rowcount == 0 and item.purpose == ItemPurpose.VERIFICATION:
        exposure = session.get(ItemExposure, (student_id, fingerprint))
        if exposure.task_id != task_id or exposure.item_id != item.id:
            raise VerificationExposureConflict("验证题的指纹与该学生已展示的题目重复")


def create_question_photo(
    session: Session,
    student_id: str,
    data_url: str,
    *,
    task_id: int | None = None,
    class_id: str | None = None,
    created_on: date | None = None,
) -> QuestionPhoto:
    photo = QuestionPhoto(
        id=uuid4().hex,
        student_id=student_id,
        task_id=task_id,
        data_url=data_url,
        class_id=class_id,
        created_on=created_on or date.today(),
    )
    session.add(photo)
    session.flush()
    return photo


def bind_question_photos(
    session: Session, student_id: str, task_id: int, photo_ids: list[str]
) -> list[QuestionPhoto]:
    """Attach unbound photos to a task, rejecting missing or foreign photos."""
    photos = []
    task = session.get(Task, task_id)
    for photo_id in dict.fromkeys(photo_ids):
        photo = session.get(QuestionPhoto, photo_id)
        if photo is None or photo.student_id != student_id or photo.class_id != task.class_id:
            raise ValueError("题目图片不存在或不属于当前学生")
        if photo.task_id not in (None, task_id):
            raise ValueError("题目图片已用于其他提问")
        photo.task_id = task_id
        session.add(photo)
        photos.append(photo)
    session.flush()
    return photos


def question_photo_urls(
    session: Session,
    student_id: str,
    photo_ids: list[str],
    *,
    task_id: int | None = None,
) -> list[str]:
    urls = []
    task = session.get(Task, task_id) if task_id is not None else None
    for photo_id in dict.fromkeys(photo_ids):
        photo = session.get(QuestionPhoto, photo_id)
        if (
            photo is None
            or photo.student_id != student_id
            or (task is not None and photo.class_id != task.class_id)
        ):
            raise ValueError("题目图片不存在或不属于当前学生")
        if photo.task_id not in (None, task_id):
            raise ValueError("题目图片不属于当前题目")
        if task_id is not None and photo.task_id is None:
            photo.task_id = task_id
            session.add(photo)
        urls.append(photo.data_url)
    return urls


def task_question_photo_urls(
    session: Session, student_id: str, task_id: int, *, limit: int = 4
) -> list[str]:
    task = session.get(Task, task_id)
    rows = list(
        session.exec(
            select(QuestionPhoto)
            .where(
                QuestionPhoto.student_id == student_id,
                QuestionPhoto.task_id == task_id,
                QuestionPhoto.class_id == task.class_id,
            )
            .order_by(QuestionPhoto.created_at, QuestionPhoto.id)
        )
    )
    if len(rows) > limit:
        # Always keep the original problem photo, then the most recent supplements.
        kept = rows[:1] + (rows[-(limit - 1) :] if limit > 1 else [])
        rows = kept
    return [row.data_url for row in rows]
