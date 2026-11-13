"""Persistent class ownership and the shared authorization rules."""

from datetime import date

from sqlalchemy import CheckConstraint, Index, event, false, or_, text
from sqlalchemy.orm import with_loader_criteria
from sqlmodel import Field, Session, SQLModel, select

from app.contracts.entities import (
    Event,
    Evidence,
    Hypothesis,
    Item,
    Mastery,
    Misconception,
    Student,
    Task,
    TeachingGoal,
    _json_list,
)
from app.contracts.enums import ContentSource, ItemSource, MasteryState


class Classroom(SQLModel, table=True):
    __tablename__ = "classroom"

    id: str = Field(primary_key=True)
    teacher_id: str = Field(index=True)
    name: str


class Enrollment(SQLModel, table=True):
    __tablename__ = "enrollment"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'left')"),
        Index(
            "uq_active_student", "student_id", unique=True, sqlite_where=text("status = 'active'")
        ),
        Index("uq_class_student_number", "class_id", "student_number", unique=True),
    )

    class_id: str = Field(foreign_key="classroom.id", primary_key=True)
    student_id: str = Field(foreign_key="student.id", primary_key=True, index=True)
    student_number: str | None = None
    status: str = "active"


class StudentPlacement(SQLModel, table=True):
    """Only this teacher can re-enroll an existing student; changed by maintenance on transfer."""

    __tablename__ = "student_placement"

    student_id: str = Field(foreign_key="student.id", primary_key=True)
    teacher_id: str


def owned_classes(teacher_id: str | None):
    return select(Classroom.id).where(Classroom.teacher_id == teacher_id)


def active_students(class_ids):
    return select(Enrollment.student_id).where(
        Enrollment.class_id.in_(class_ids), Enrollment.status == "active"
    )


def active_class(session: Session, student_id: str) -> str | None:
    return session.exec(
        select(Enrollment.class_id).where(
            Enrollment.student_id == student_id, Enrollment.status == "active"
        )
    ).first()


def may_work(session: Session, task: Task) -> bool:
    if task.student_id is None:
        return True
    if task.class_id is not None:
        return active_class(session, task.student_id) == task.class_id
    # Standalone legacy accounts still see their records. Once enrolled, old
    # unassigned tasks are read-only until a maintainer assigns their origin.
    return session.get(StudentPlacement, task.student_id) is None


def can_see(session: Session, principal, record: Task | Evidence | None) -> bool:
    if record is None:
        return False
    if principal.role == "student":
        return principal.student_id is not None and record.student_id == principal.student_id
    if record.class_id is None:
        return False
    return (
        session.exec(
            owned_classes(principal.teacher_id).where(Classroom.id == record.class_id)
        ).first()
        is not None
    )


def write_lock(session: Session) -> None:
    """Serialize membership and credential changes with SQLite writers."""
    session.execute(text("BEGIN IMMEDIATE"))


class ClassMastery(SQLModel, table=True):
    __tablename__ = "class_mastery"

    class_id: str = Field(foreign_key="classroom.id", primary_key=True)
    student_id: str = Field(foreign_key="student.id", primary_key=True)
    knowledge_point_id: str = Field(foreign_key="knowledge_point.id", primary_key=True)
    state: MasteryState
    evidence_ids: list[int] = _json_list(int)
    updated_on: date


def mastery_for(session: Session, student_id: str, kp_id: str, class_id: str | None):
    if class_id is not None:
        return session.get(ClassMastery, (class_id, student_id, kp_id))
    return session.get(Mastery, (student_id, kp_id))


def mastery_rows(session: Session, student_id: str | None = None):
    model = ClassMastery if session.info.get("class_id") is not None else Mastery
    query = select(model)
    return query.where(model.student_id == student_id) if student_id is not None else query


class ClassSession(Session):
    """Limit class read models and planner discovery, including by-primary-key lookups."""

    def __init__(self, engine, class_id: str | None, *, active_only: bool = True, **kwargs):
        super().__init__(engine, **kwargs)
        self.info.update(class_id=class_id, active_only=active_only)


@event.listens_for(ClassSession, "do_orm_execute")
def _scope_select(state):
    if not state.is_select:
        return
    class_id = state.session.info["class_id"]
    active_only = state.session.info["active_only"]
    options = []
    for model in (Task, Event, Evidence, TeachingGoal, ClassMastery):
        options.append(with_loader_criteria(model, model.class_id == class_id))
    if class_id is not None:
        task_ids = select(Task.id).where(Task.class_id == class_id)
        roster = active_students([class_id])
        options.extend(
            (
                with_loader_criteria(Hypothesis, Hypothesis.task_id.in_(task_ids)),
                with_loader_criteria(
                    Misconception,
                    or_(
                        Misconception.class_id == class_id,
                        Misconception.class_id.is_(None)
                        & (Misconception.source == ContentSource.TEACHER),
                    ),
                ),
                with_loader_criteria(
                    Item,
                    or_(
                        Item.class_id == class_id,
                        Item.class_id.is_(None) & (Item.source == ItemSource.SEED),
                    ),
                ),
                with_loader_criteria(Mastery, false()),
            )
        )
        if active_only:
            options.append(with_loader_criteria(Student, Student.id.in_(roster)))
            for model in (Task, Evidence, ClassMastery):
                criterion = model.student_id.in_(roster)
                if model is Task:
                    criterion = or_(model.student_id.is_(None), criterion)
                options.append(with_loader_criteria(model, criterion))
            options.append(with_loader_criteria(Hypothesis, Hypothesis.student_id.in_(roster)))
    else:
        options.extend(
            (
                with_loader_criteria(
                    Student, Student.id.not_in(select(StudentPlacement.student_id))
                ),
                with_loader_criteria(
                    Mastery, Mastery.student_id.not_in(select(StudentPlacement.student_id))
                ),
                with_loader_criteria(
                    Hypothesis,
                    Hypothesis.task_id.in_(select(Task.id).where(Task.class_id.is_(None))),
                ),
                with_loader_criteria(Item, Item.class_id.is_(None)),
                with_loader_criteria(Misconception, Misconception.class_id.is_(None)),
            )
        )
    state.statement = state.statement.options(*options)
