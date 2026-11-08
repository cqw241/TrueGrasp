"""T07 私有存储；不扩展公共实体或 HTTP 契约。"""

from datetime import date

from sqlmodel import Field, SQLModel


class TeachingClockState(SQLModel, table=True):
    __tablename__ = "teaching_clock_state"

    id: int = Field(default=1, primary_key=True)
    teaching_date: date
    lecture: int = 1


class DueNotification(SQLModel, table=True):
    __tablename__ = "clock_due_notification"

    task_id: int = Field(primary_key=True, foreign_key="task.id")
    event_id: int = Field(foreign_key="event.id")


class LectureEntry(SQLModel, table=True):
    __tablename__ = "clock_lecture_entry"

    lecture: int = Field(primary_key=True)
    event_id: int = Field(foreign_key="event.id")


class PrecheckContext(SQLModel, table=True):
    __tablename__ = "clock_precheck_context"

    lecture: int = Field(primary_key=True)
    student_id: str = Field(primary_key=True, foreign_key="student.id")
    knowledge_point_id: str = Field(primary_key=True, foreign_key="knowledge_point.id")
    context_knowledge_point_id: str = Field(primary_key=True, foreign_key="knowledge_point.id")
    task_id: int = Field(foreign_key="task.id", unique=True, index=True)
