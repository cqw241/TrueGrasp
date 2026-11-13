"""Idempotent publication, reusing the importer and its existing event outbox."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from openai import OpenAIError
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from app.classroom import ClassSession, write_lock
from app.homework.models import (
    Extraction,
    HomeworkDraft,
    Publication,
    PublicationError,
    PublishSelection,
    RecordResult,
    Workflow,
)
from app.homework.review import (
    ReviewError,
    confirmed_rows,
    inspect_selection,
    owned_draft,
    require_complete,
)
from app.homework.service import UploadError, parser_client
from app.homework.storage import DraftRow, PublicationRow, PublishedRecord
from app.ingest.service import HomeworkImporter, IngestError
from app.llm.client import LLMOutputError

logger = logging.getLogger(__name__)


def signature(draft_id: str, body: PublishSelection) -> str:
    return hashlib.sha256(
        json.dumps(
            [draft_id, body.expected_revision, sorted(body.record_ids)], ensure_ascii=False
        ).encode()
    ).hexdigest()


def projection(runtime, row: PublicationRow) -> Publication:
    publication = Publication.model_validate(row.payload)
    if publication.import_result:
        publication.workflow = Workflow.model_validate(
            runtime.event_workflow(publication.import_result.event_id)
        )
    return publication


def fail_publication(engine, publication_id: str, error: ReviewError) -> Publication:
    with Session(engine) as session:
        write_lock(session)
        row = session.get(PublicationRow, publication_id)
        publication = Publication.model_validate(row.payload)
        if row.status != "publishing":
            return publication
        publication.status = "failed"
        publication.error = PublicationError(
            code=error.detail["code"], message=str(error), retryable=error.status >= 500
        )
        row.status = "failed"
        row.failure_status = error.status
        row.payload = publication.model_dump(mode="json")
        draft_row = session.get(DraftRow, row.draft_id)
        draft = HomeworkDraft.model_validate(draft_row.payload)
        if draft.active_publication_id == publication_id:
            draft.active_publication_id = None
            draft.updated_at = datetime.now(UTC)
            draft_row.payload = draft.model_dump(mode="json")
            session.add(draft_row)
        session.add(row)
        session.commit()
        return publication


def interrupt_publications(engine) -> None:
    """Single-instance startup: no old executor remains; an uncommitted claim may be released."""
    with Session(engine) as session:
        ids = list(
            session.exec(select(PublicationRow.id).where(PublicationRow.status == "publishing"))
        )
    for publication_id in ids:
        fail_publication(
            engine,
            publication_id,
            ReviewError(503, "publish_interrupted", "服务重启中断发布，未正式导入；请重新发布"),
        )


async def publish(
    runtime, clock, principal, draft_id: str, body: PublishSelection, key: str, injected=None
) -> tuple[Publication, int]:
    engine = runtime.engine
    fingerprint = signature(draft_id, body)
    with Session(engine) as session:
        write_lock(session)
        draft_row = owned_draft(session, principal, draft_id)
        existing = session.exec(
            select(PublicationRow).where(
                PublicationRow.teacher_id == principal.teacher_id,
                PublicationRow.idempotency_key == key,
            )
        ).first()
        if existing:
            if existing.draft_id != draft_id or existing.request_hash != fingerprint:
                raise ReviewError(
                    409, "idempotency_conflict", "该发布键已用于其他内容，请重新核对后发起发布"
                )
            return projection(runtime, existing), (
                202 if existing.status == "publishing" else existing.failure_status or 200
            )
        draft = HomeworkDraft.model_validate(draft_row.payload)
        require_complete(inspect_selection(session, draft, body))
        record_ids = sorted(body.record_ids)
        selected = [r.model_copy(deep=True) for r in draft.records if r.record_id in record_ids]
        question_ids = {r.question_id for r in selected}
        publication = Publication(
            publication_id="pub_" + uuid4().hex,
            draft_id=draft_id,
            class_id=draft.class_id,
            confirmed_revision=draft.revision,
            record_ids=record_ids,
            confirmed_by_teacher_id=principal.teacher_id,
            confirmed_at=datetime.now(UTC),
            teaching_date=clock.state(draft.class_id).teaching_date,
            status="publishing",
            confirmed_snapshot=Extraction(
                questions=[
                    q.model_copy(deep=True)
                    for q in draft.questions
                    if q.question_id in question_ids
                ],
                records=selected,
            ),
        )
        session.add(
            PublicationRow(
                id=publication.publication_id,
                draft_id=draft_id,
                teacher_id=principal.teacher_id,
                idempotency_key=key,
                request_hash=fingerprint,
                status="publishing",
                payload=publication.model_dump(mode="json"),
            )
        )
        draft.active_publication_id = publication.publication_id
        draft.publication_ids.append(publication.publication_id)
        draft.updated_at = datetime.now(UTC)
        draft_row.payload = draft.model_dump(mode="json")
        session.add(draft_row)
        session.commit()
    publication_id = publication.publication_id
    try:
        async with parser_client(injected, vision=False) as llm:
            importer = HomeworkImporter(engine, llm)
            rows = confirmed_rows(draft, publication.record_ids)
            prepared = await importer.prepare_rows(
                rows, publication.teaching_date, class_id=publication.class_id
            )
            with ClassSession(engine, publication.class_id) as session:
                write_lock(session)
                row = owned_draft(session, principal, draft_id)
                current = HomeworkDraft.model_validate(row.payload)
                if current.active_publication_id != publication_id:
                    raise ReviewError(
                        409, "publish_in_progress", "发布占用已变化，请刷新后查看回执"
                    )
                require_complete(
                    inspect_selection(
                        session, current.model_copy(update={"active_publication_id": None}), body
                    )
                )
                result, evidence_ids = importer.write_prepared(
                    session, prepared, publication.teaching_date
                )
                publication.import_result = result
                publication.status = "published"
                publication.workflow = Workflow(event_id=result.event_id)
                publication.record_results = [
                    RecordResult(record_id=rid, item_id=entry.item_id, evidence_id=eid)
                    for rid, entry, eid in zip(
                        publication.record_ids, rows, evidence_ids, strict=True
                    )
                ]
                for mapping in publication.record_results:
                    session.add(
                        PublishedRecord(
                            draft_id=draft_id,
                            record_id=mapping.record_id,
                            publication_id=publication_id,
                            evidence_id=mapping.evidence_id,
                            item_id=mapping.item_id,
                        )
                    )
                for record in current.records:
                    if record.record_id in publication.record_ids:
                        record.review_status = "published"
                        record.publication_id = publication_id
                current.active_publication_id = None
                current.revision += 1
                current.updated_at = datetime.now(UTC)
                current.status = (
                    "published"
                    if all(r.review_status in ("published", "skipped") for r in current.records)
                    else "review"
                )
                row.payload = current.model_dump(mode="json")
                receipt = session.get(PublicationRow, publication_id)
                receipt.status = "published"
                receipt.payload = publication.model_dump(mode="json")
                session.add(row)
                session.add(receipt)
                session.commit()
    except (
        ReviewError,
        IngestError,
        UploadError,
        OpenAIError,
        LLMOutputError,
        IntegrityError,
        OperationalError,
    ) as exc:
        if isinstance(exc, ReviewError):
            error = exc
        elif isinstance(exc, IngestError):
            error = ReviewError(409, "import_conflict", str(exc))
        elif isinstance(exc, IntegrityError):
            error = ReviewError(
                409, "import_conflict", "数据与现有记录冲突，本次未导入，请刷新核对"
            )
        elif isinstance(exc, OperationalError):
            error = ReviewError(
                503, "storage_unavailable", "存储暂不可用，请先查询发布回执确认结果"
            )
        elif isinstance(exc, UploadError):
            error = ReviewError(exc.status, "import_model_failed", str(exc))
        else:
            error = ReviewError(
                502, "import_model_failed", "作业模型分析失败，本次未导入；请稍后重新发布"
            )
        try:
            outcome = fail_publication(engine, publication_id, error)
        except OperationalError as storage_error:
            raise ReviewError(
                503,
                "storage_unavailable",
                "暂不能确认发布结果，请保留当前请求并查询回执",
                publication_id=publication_id,
            ) from storage_error
        if outcome.status != "published":
            return outcome, error.status
        publication = outcome
    except Exception:
        logger.exception("发布失败 publication=%s", publication_id)
        outcome = fail_publication(
            engine,
            publication_id,
            ReviewError(500, "publish_failed", "发布未完成，请查看回执并重试"),
        )
        if outcome.status != "published":
            return outcome, 500
        publication = outcome
    # The outbox is already committed. A wake-up failure must not reverse publication.
    try:
        runtime.recover_pending()
    except Exception:
        logger.exception("作业已发布，待办唤醒失败 publication=%s", publication_id)
    with Session(engine) as session:
        return projection(runtime, session.get(PublicationRow, publication_id)), 201
