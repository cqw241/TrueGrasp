"""OPT-03：教师个人草稿。班级匹配及审核发布由后续任务接入。"""

import hashlib
import json
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app.homework.models import (
    DraftApiError,
    DraftSource,
    DraftSummary,
    HomeworkDraft,
    RetrySources,
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
from app.homework.storage import DraftRow, SourceData

router = APIRouter(
    tags=["homework"],
    responses={code: {"model": DraftApiError} for code in (401, 403, 404, 409, 413, 415, 422)},
)


def problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status, {"code": code, "message": message, "retryable": False, "issues": []}
    )


def owned(session: Session, draft_id: str, request: Request) -> DraftRow:
    row = session.get(DraftRow, draft_id)
    if row is None or row.owner != request.state.principal.username:
        raise problem(404, "draft_not_found", "草稿不存在")
    return row


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
def list_drafts(request: Request) -> list[DraftSummary]:
    with Session(request.app.state.orchestrator.engine) as session:
        rows = session.exec(
            select(DraftRow).where(DraftRow.owner == request.state.principal.username)
        )
        summaries = [DraftSummary.model_validate(row.payload) for row in rows]
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)


@router.post("/homework-drafts", status_code=202)
async def create_draft(
    request: Request,
    background: BackgroundTasks,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    files: list[UploadFile] | None = None,
    title: Annotated[str, Form(max_length=200)] = "作业解析",
    prompt_text: Annotated[str | None, Form()] = None,
) -> HomeworkDraft:
    sources = await read_sources(files or [], prompt_text)
    fingerprint = hashlib.sha256(
        json.dumps(
            [title, [(s.filename, s.sha256) for s, _ in sources]], ensure_ascii=False
        ).encode()
    ).hexdigest()
    owner = request.state.principal.username
    draft = HomeworkDraft(draft_id=uuid4().hex, created_by_username=owner, title=title)
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
            return HomeworkDraft.model_validate(existing.payload)
    enqueue(background, request, draft.draft_id)
    return draft


@router.get("/homework-drafts/{draft_id}")
def get_draft(draft_id: str, request: Request) -> HomeworkDraft:
    with Session(request.app.state.orchestrator.engine) as session:
        return HomeworkDraft.model_validate(owned(session, draft_id, request).payload)


def claim(session: Session, row: DraftRow, expected_revision: int) -> HomeworkDraft:
    draft = HomeworkDraft.model_validate(row.payload)
    if draft.status == "parsing" or draft.revision != expected_revision:
        raise problem(409, "revision_conflict", "草稿正在解析或版本已更新，请刷新后重试")
    draft.status = "parsing"
    draft.revision += 1
    result = session.exec(
        update(DraftRow)
        .where(
            DraftRow.id == row.id,
            DraftRow.payload["revision"].as_integer() == expected_revision,
            DraftRow.payload["status"].as_string() != "parsing",
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
