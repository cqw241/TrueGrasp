"""草稿及原文件存在现有业务库，避免新增文件服务与孤立文件清理。"""

from datetime import UTC, datetime

from sqlalchemy import JSON, Column, LargeBinary, UniqueConstraint
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
