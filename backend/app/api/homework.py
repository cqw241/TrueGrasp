"""教师作业上传、审核与确认发布。"""

import hashlib
import json
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app.classroom import Enrollment, mastery_for, may_work, owned_classes, write_lock
from app.contracts.api import DimensionResult
from app.contracts.entities import Evidence, KnowledgePoint, Student, Task
from app.contracts.enums import Dimension, EvidenceKind
from app.homework.models import (
    DraftApiError,
    DraftSource,
    DraftSummary,
    DraftUpdate,
    HomeworkDraft,
    HomeworkTaskProgress,
    Publication,
    PublicationProgress,
    PublishPreview,
    PublishSelection,
    RetrySources,
)
from app.homework.publication import projection, publish
from app.homework.review import (
    ReviewError,
    check_class,
    inspect_selection,
    owned_draft,
    update_draft,
)
from app.homework.service import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TEXT_CHARS,
    MAX_TOTAL_BYTES,
    MAX_UNITS,
    UploadError,
    parse_draft,
    prepare_source,
)
from app.homework.storage import DraftRow, PublicationRow, SourceData
from app.orchestration.storage import (
    PendingWork,
    WorkFailure,
    teacher_has_student,
)

router = APIRouter(
    tags=["homework"],
    responses={code: {"model": DraftApiError} for code in (401, 403, 404, 409, 413, 415, 422)},
)


def problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status, {"code": code, "message": message, "retryable": False, "issues": []}
    )


def owned(session: Session, draft_id: str, request: Request) -> DraftRow:
    return owned_draft(session, request.state.principal, draft_id)


async def read_sources(files: list[UploadFile], prompt_text: str | None = None):
    if len(files) > MAX_FILES:
        raise problem(413, "too_many_files", f"每份草稿最多 {MAX_FILES} 个文件")
    sources = []
    total = 0
    try:
        for file in files:
            data = await file.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise UploadError(413, "文件总大小不能超过 30 MiB")
            source = await run_in_threadpool(prepare_source, file.filename or "作业", data)
            sources.append((source, data))
    except UploadError as exc:
        raise problem(exc.status, "invalid_upload", str(exc)) from exc
    finally:
        for file in files:
            await file.close()
    if prompt_text and prompt_text.strip():
        text = prompt_text.strip()
        if len(text) > MAX_TEXT_CHARS:
            raise problem(413, "text_too_large", "输入文字不能超过 60000 字符")
        data = text.encode("utf-8")
        sources.append(
            (
                DraftSource(
                    source_id=uuid4().hex,
                    kind="text",
                    filename="输入的题目与作答.txt",
                    mime_type="text/plain",
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                ),
                data,
            )
        )
    if not sources:
        raise problem(422, "input_required", "请选择文件或输入题目与作答")
    if sum(source.size_bytes for source, _ in sources) > MAX_TOTAL_BYTES:
        raise problem(413, "upload_too_large", "材料总大小不能超过 30 MiB")
    if sum(source.page_count or 1 for source, _ in sources) > MAX_UNITS:
        raise problem(413, "too_many_pages", "每份草稿最多解析 24 页/份材料，请拆分后上传")
    return sources


def enqueue(background: BackgroundTasks, request: Request, draft_id: str, source_ids=None):
    background.add_task(
        parse_draft,
        request.app.state.orchestrator.engine,
        draft_id,
        getattr(request.app.state, "homework_parser_llm", None),
        source_ids,
    )


@router.get("/homework-drafts")
def list_drafts(request: Request, class_id: str | None = None) -> list[DraftSummary]:
    principal = request.state.principal
    with Session(request.app.state.orchestrator.engine) as session:
        if class_id is not None:
            check_class(session, principal, class_id)
        rows = session.exec(
            select(DraftRow).where(
                (DraftRow.payload["class_id"].as_string().in_(owned_classes(principal.teacher_id)))
                | (
                    (DraftRow.payload["class_id"].as_string().is_(None))
                    & (DraftRow.owner == principal.username)
                )
            )
        )
        summaries = [
            DraftSummary.model_validate(row.payload)
            for row in rows
            if class_id is None or row.payload.get("class_id") in (None, class_id)
        ]
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)


@router.post("/homework-drafts", status_code=202)
async def create_draft(
    request: Request,
    background: BackgroundTasks,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    files: list[UploadFile] | None = None,
    title: Annotated[str, Form(max_length=200)] = "作业解析",
    prompt_text: Annotated[str | None, Form()] = None,
    class_id: Annotated[str | None, Form()] = None,
) -> HomeworkDraft:
    if class_id is not None:
        with Session(request.app.state.orchestrator.engine) as session:
            check_class(session, request.state.principal, class_id)
    sources = await read_sources(files or [], prompt_text)
    fingerprint = hashlib.sha256(
        json.dumps(
            [title, class_id, [(s.filename, s.sha256) for s, _ in sources]], ensure_ascii=False
        ).encode()
    ).hexdigest()
    owner = request.state.principal.username
    draft = HomeworkDraft(
        draft_id=uuid4().hex,
        created_by_username=owner,
        title=title,
        class_id=class_id,
        created_by_teacher_id=request.state.principal.teacher_id,
    )
    draft.sources = [s for s, _ in sources]
    with Session(request.app.state.orchestrator.engine) as session:
        existing = session.exec(
            select(DraftRow).where(
                DraftRow.owner == owner, DraftRow.idempotency_key == idempotency_key
            )
        ).first()
        if existing is None:
            row = DraftRow(
                id=draft.draft_id,
                owner=owner,
                idempotency_key=idempotency_key,
                request_hash=fingerprint,
                payload=draft.model_dump(mode="json"),
            )
            try:
                session.add(row)
                session.flush()
                session.add_all(
                    SourceData(id=s.source_id, draft_id=draft.draft_id, data=data)
                    for s, data in sources
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.exec(
                    select(DraftRow).where(
                        DraftRow.owner == owner, DraftRow.idempotency_key == idempotency_key
                    )
                ).one()
        if existing is not None:
            if existing.request_hash != fingerprint:
                raise problem(409, "idempotency_conflict", "重复请求的内容已变化，请重新发起上传")
            owned(session, existing.id, request)
            return HomeworkDraft.model_validate(existing.payload)
    enqueue(background, request, draft.draft_id)
    return draft


@router.get("/homework-drafts/{draft_id}")
def get_draft(draft_id: str, request: Request) -> HomeworkDraft:
    with Session(request.app.state.orchestrator.engine) as session:
        return HomeworkDraft.model_validate(owned(session, draft_id, request).payload)


def claim(session: Session, row: DraftRow, expected_revision: int) -> HomeworkDraft:
    draft = HomeworkDraft.model_validate(row.payload)
    if (
        draft.status == "parsing"
        or draft.active_publication_id
        or draft.revision != expected_revision
    ):
        raise problem(409, "revision_conflict", "草稿正在解析或版本已更新，请刷新后重试")
    draft.status = "parsing"
    draft.revision += 1
    result = session.exec(
        update(DraftRow)
        .where(
            DraftRow.id == row.id,
            DraftRow.payload["revision"].as_integer() == expected_revision,
            DraftRow.payload["status"].as_string() != "parsing",
            DraftRow.payload["active_publication_id"].as_string().is_(None),
        )
        .values(payload=draft.model_dump(mode="json"))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise problem(409, "revision_conflict", "草稿版本已更新，请刷新后重试")
    return draft


@router.post("/homework-drafts/{draft_id}/parse-retries", status_code=202)
def retry_sources(
    draft_id: str, body: RetrySources, request: Request, background: BackgroundTasks
) -> HomeworkDraft:
    with Session(request.app.state.orchestrator.engine) as session:
        row = owned(session, draft_id, request)
        current = HomeworkDraft.model_validate(row.payload)
        failed = {error.source_id for error in current.parse_errors if error.retryable}
        if not set(body.source_ids) <= failed:
            raise problem(422, "not_failed_source", "只能重试解析失败的文件")
        draft = claim(session, row, body.expected_revision)
        session.commit()
    enqueue(background, request, draft_id, body.source_ids)
    return draft


@router.post("/homework-drafts/{draft_id}/sources", status_code=202)
async def add_sources(
    draft_id: str,
    request: Request,
    background: BackgroundTasks,
    files: list[UploadFile],
    expected_revision: Annotated[int, Form()],
) -> HomeworkDraft:
    # 在读取文件之前先确认资源归属。
    with Session(request.app.state.orchestrator.engine) as session:
        owned(session, draft_id, request)
    sources = await read_sources(files)
    with Session(request.app.state.orchestrator.engine) as session:
        row = owned(session, draft_id, request)
        draft = claim(session, row, expected_revision)
        all_sources = draft.sources + [s for s, _ in sources]
        if (
            sum(s.kind == "file" for s in all_sources) > MAX_FILES
            or sum(s.size_bytes for s in all_sources) > MAX_TOTAL_BYTES
            or sum(s.page_count or 1 for s in all_sources) > MAX_UNITS
        ):
            raise problem(413, "draft_too_large", "草稿超过 6 个文件、30 MiB 或 24 页材料的上限")
        draft.sources = all_sources
        session.exec(
            update(DraftRow)
            .where(DraftRow.id == draft_id)
            .values(payload=draft.model_dump(mode="json"))
        )
        session.add_all(
            SourceData(id=s.source_id, draft_id=draft_id, data=data) for s, data in sources
        )
        session.commit()
    enqueue(background, request, draft_id, [s.source_id for s, _ in sources])
    return draft


@router.get("/homework-drafts/{draft_id}/sources/{source_id}")
def get_source(draft_id: str, source_id: str, request: Request) -> Response:
    with Session(request.app.state.orchestrator.engine) as session:
        row = owned(session, draft_id, request)
        draft = HomeworkDraft.model_validate(row.payload)
        source = next((s for s in draft.sources if s.source_id == source_id), None)
        if source is None:
            raise problem(404, "source_not_found", "文件不存在")
        data = session.get(SourceData, source_id)
        return Response(
            data.data,
            media_type=source.mime_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline"
                if source.mime_type.startswith("image/") or source.mime_type == "application/pdf"
                else "attachment",
            },
        )


@router.patch("/homework-drafts/{draft_id}")
def patch_draft(draft_id: str, body: DraftUpdate, request: Request) -> HomeworkDraft:
    with Session(request.app.state.orchestrator.engine) as session:
        write_lock(session)
        row = owned(session, draft_id, request)
        draft = update_draft(session, request.state.principal, row, body)
        session.commit()
        return draft


@router.post("/homework-drafts/{draft_id}/publish-preview")
def preview_publication(draft_id: str, body: PublishSelection, request: Request) -> PublishPreview:
    with Session(request.app.state.orchestrator.engine) as session:
        draft = HomeworkDraft.model_validate(owned(session, draft_id, request).payload)
        return inspect_selection(session, draft, body)


@router.post(
    "/homework-drafts/{draft_id}/publish",
    status_code=201,
    responses={
        202: {"model": Publication},
        200: {"model": Publication},
        502: {"model": DraftApiError},
        503: {"model": DraftApiError},
    },
)
async def publish_draft(
    draft_id: str,
    body: PublishSelection,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> Publication:
    result, status = await publish(
        request.app.state.orchestrator,
        request.app.state.clock,
        request.state.principal,
        draft_id,
        body,
        idempotency_key,
        getattr(request.app.state, "homework_llm", None),
    )
    if status >= 400:
        return JSONResponse(
            status_code=status,
            content={
                "detail": {
                    **result.error.model_dump(mode="json"),
                    "publication_id": result.publication_id,
                },
                "publication": result.model_dump(mode="json"),
            },
        )
    response.status_code = status
    response.headers["Location"] = f"/api/homework-publications/{result.publication_id}"
    return result


@router.get("/homework-publications/{publication_id}")
def get_publication(publication_id: str, request: Request) -> Publication:
    runtime = request.app.state.orchestrator
    with Session(runtime.engine) as session:
        row = session.get(PublicationRow, publication_id)
        if row is None:
            raise ReviewError(404, "publication_not_found", "发布记录不存在")
        owned(session, row.draft_id, request)
        return projection(runtime, row)


def authorized_publication(session: Session, publication_id: str, request: Request) -> Publication:
    row = session.get(PublicationRow, publication_id)
    if row is None:
        raise ReviewError(404, "publication_not_found", "发布记录不存在")
    owned(session, row.draft_id, request)
    publication = Publication.model_validate(row.payload)
    check_class(session, request.state.principal, publication.class_id)
    return publication


@router.post("/homework-publications/{publication_id}/workflow-retries", status_code=202)
async def retry_publication_workflow(publication_id: str, request: Request) -> Publication:
    runtime = request.app.state.orchestrator
    with Session(runtime.engine) as session:
        publication = authorized_publication(session, publication_id, request)
        if publication.status != "published" or publication.import_result is None:
            raise ReviewError(409, "not_published", "作业尚未成功导入，请先查看发布回执")
    await runtime.retry_homework(publication.import_result.event_id)
    return get_publication(publication_id, request)


@router.get("/homework-publications/{publication_id}/progress")
def get_publication_progress(publication_id: str, request: Request) -> PublicationProgress:
    runtime = request.app.state.orchestrator
    with Session(runtime.engine) as session:
        publication = authorized_publication(session, publication_id, request)
        row = session.get(PublicationRow, publication_id)
        workflow = projection(runtime, row).workflow
        tasks = session.exec(
            select(Task)
            .where(Task.id.in_(workflow.task_ids), Task.class_id == publication.class_id)
            .order_by(Task.id)
        ).all()
        progress = []
        for task in tasks:
            student = session.get(Student, task.student_id) if task.student_id else None
            membership = (
                session.get(Enrollment, (publication.class_id, task.student_id))
                if task.student_id
                else None
            )
            point = session.get(KnowledgePoint, task.knowledge_point_id)
            mastery = (
                mastery_for(session, task.student_id, task.knowledge_point_id, publication.class_id)
                if task.student_id
                else None
            )
            evidence = session.exec(
                select(Evidence)
                .where(
                    Evidence.task_id == task.id,
                    Evidence.class_id == publication.class_id,
                    Evidence.kind == EvidenceKind.VERIFICATION,
                )
                .order_by(Evidence.id)
            ).all()
            latest = {entry.dimension: entry for entry in evidence}
            work = session.get(PendingWork, f"task:{task.id}")
            progress.append(
                HomeworkTaskProgress(
                    task=task,
                    student_name=student.name if student else None,
                    student_number=membership.student_number if membership else None,
                    knowledge_point_name=point.name if point else task.knowledge_point_id,
                    mastery_state=mastery.state if mastery else None,
                    verification=[
                        DimensionResult(
                            dimension=dimension,
                            passed=entry.is_correct if (entry := latest.get(dimension)) else None,
                            judged_by=entry.judged_by if entry else None,
                            evidence_id=entry.id if entry else None,
                        )
                        for dimension in Dimension
                    ],
                    failed=bool(work and work.pending and session.get(WorkFailure, work.key)),
                    teacher_takeover=teacher_has_student(session, task.student_id, task.class_id),
                    can_work=may_work(session, task),
                )
            )
        return PublicationProgress(
            publication_id=publication_id,
            class_id=publication.class_id,
            workflow=workflow,
            tasks=progress,
        )
