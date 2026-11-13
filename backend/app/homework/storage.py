"""草稿及原文件存在现有业务库，避免新增文件服务与孤立文件清理。"""

from datetime import UTC, datetime

from sqlalchemy import JSON, Column, Index, LargeBinary, UniqueConstraint, text
from sqlmodel import Field, Session, SQLModel, select

from app.homework.models import HomeworkDraft, ParseError


class DraftRow(SQLModel, table=True):
    __tablename__ = "homework_draft"
    __table_args__ = (UniqueConstraint("owner", "idempotency_key"),)

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)
    idempotency_key: str
    request_hash: str
    payload: dict = Field(sa_column=Column(JSON, nullable=False))


class SourceData(SQLModel, table=True):
    __tablename__ = "homework_source"

    id: str = Field(primary_key=True)
    draft_id: str = Field(foreign_key="homework_draft.id", index=True)
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))


class PublicationRow(SQLModel, table=True):
    __tablename__ = "homework_publication"
    __table_args__ = (
        UniqueConstraint("teacher_id", "idempotency_key"),
        Index(
            "uq_homework_publishing",
            "draft_id",
            unique=True,
            sqlite_where=text("status = 'publishing'"),
        ),
    )

    id: str = Field(primary_key=True)
    draft_id: str = Field(foreign_key="homework_draft.id", index=True)
    teacher_id: str
    idempotency_key: str
    request_hash: str
    status: str
    failure_status: int | None = None
    payload: dict = Field(sa_column=Column(JSON, nullable=False))


class PublishedRecord(SQLModel, table=True):
    __tablename__ = "homework_published_record"

    draft_id: str = Field(foreign_key="homework_draft.id", primary_key=True)
    record_id: str = Field(primary_key=True)
    publication_id: str = Field(foreign_key="homework_publication.id", index=True)
    evidence_id: int = Field(foreign_key="evidence.id", unique=True)
    item_id: str = Field(foreign_key="item.id")


def normalize_drafts(engine) -> None:
    """Persist defaults once, so historical issue IDs stay stable across reads."""
    with Session(engine) as session:
        for row in session.exec(select(DraftRow)):
            normalized = HomeworkDraft.model_validate(row.payload).model_dump(mode="json")
            if normalized != row.payload:
                row.payload = normalized
                session.add(row)
        session.commit()


def save(session: Session, row: DraftRow, draft: HomeworkDraft) -> None:
    draft.updated_at = datetime.now(UTC)
    row.payload = draft.model_dump(mode="json")
    session.add(row)
    session.commit()


def interrupt_unfinished(engine) -> None:
    """单实例重启：遗留解析明确失败，已完成页面仍可查看和重试。"""
    with Session(engine) as session:
        for row in session.exec(
            select(DraftRow).where(DraftRow.payload["status"].as_string() == "parsing")
        ).all():
            draft = HomeworkDraft.model_validate(row.payload)
            if draft.status != "parsing":
                continue
            completed = {
                (ref.source_id, ref.page)
                for question in draft.questions
                for ref in question.source_refs
            }
            for source in draft.sources:
                pages = range(1, (source.page_count or 1) + 1)
                for page in pages:
                    page = page if source.mime_type == "application/pdf" else None
                    if (source.source_id, page) not in completed and not any(
                        e.source_id == source.source_id and e.page == page
                        for e in draft.parse_errors
                    ):
                        draft.parse_errors.append(
                            ParseError(
                                source_id=source.source_id,
                                page=page,
                                code="interrupted",
                                message="服务重启中断了解析，请重试该文件；已解析内容已保留",
                            )
                        )
            draft.status = "review" if draft.questions else "parse_failed"
            draft.revision += 1
            save(session, row, draft)
