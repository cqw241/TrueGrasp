"""Teacher review of existing drafts; formal evidence is written only by publication."""

from datetime import UTC, datetime

from sqlmodel import Session, select

from app.classroom import Classroom, Enrollment
from app.contracts.entities import Item, KnowledgePoint, Student
from app.contracts.enums import ItemSource
from app.contracts.values import PENDING_KNOWLEDGE_POINT_ID
from app.homework.models import (
    DraftUpdate,
    Edit,
    HomeworkDraft,
    IssueResolution,
    PublishPreview,
    PublishSelection,
    ReviewProblem,
)
from app.homework.storage import DraftRow
from app.ingest.models import HomeworkRow


class ReviewError(ValueError):
    def __init__(self, status: int, code: str, message: str, *, issues=None, publication_id=None):
        super().__init__(message)
        self.status = status
        self.detail = {
            "code": code,
            "message": message,
            "retryable": status >= 500,
            "issues": issues or [],
        }
        if publication_id:
            self.detail["publication_id"] = publication_id


def check_class(session: Session, principal, class_id: str) -> Classroom:
    group = session.get(Classroom, class_id)
    if group is None or group.teacher_id != principal.teacher_id:
        raise ReviewError(404, "class_not_found", "班级不存在")
    return group


def owned_draft(session: Session, principal, draft_id: str) -> DraftRow:
    row = session.get(DraftRow, draft_id)
    if row is None:
        raise ReviewError(404, "draft_not_found", "草稿不存在")
    class_id = row.payload.get("class_id")
    if class_id is not None:
        check_class(session, principal, class_id)
    elif row.owner != principal.username:
        raise ReviewError(404, "draft_not_found", "草稿不存在")
    return row


def members(session: Session, class_id: str | None):
    return (
        list(
            session.exec(
                select(Enrollment, Student)
                .join(Student, Student.id == Enrollment.student_id)
                .where(Enrollment.class_id == class_id, Enrollment.status == "active")
            )
        )
        if class_id
        else []
    )


def propose_identities(session: Session, draft: HomeworkDraft) -> None:
    roster = members(session, draft.class_id)
    for record in draft.records:
        identity = record.identity
        if (
            identity.status == "confirmed"
            or record.review_status == "published"
            or any(e.target_id == record.record_id and e.field == "student_id" for e in draft.edits)
        ):
            continue
        candidates = []
        for value, field in (
            (identity.observed_student_id, "id"),
            (identity.student_number, "number"),
            (identity.observed_name, "name"),
        ):
            if value:
                candidates = [
                    s.id
                    for e, s in roster
                    if value == (e.student_number if field == "number" else getattr(s, field))
                ]
                if candidates:
                    break
        identity.candidate_student_ids = candidates
        identity.student_id = candidates[0] if len(candidates) == 1 else None
        identity.status = (
            "proposed" if len(candidates) == 1 else ("ambiguous" if candidates else "unmatched")
        )


def accessible_item(session: Session, class_id: str, item_id: str) -> Item | None:
    item = session.get(Item, item_id)
    if item is not None and (
        item.class_id == class_id or (item.class_id is None and item.source == ItemSource.SEED)
    ):
        return item
    return None


def item_id_for(draft: HomeworkDraft, question) -> str:
    return question.item_id or f"hw.{draft.draft_id}.{question.question_id}"


def inspect_selection(
    session: Session,
    draft: HomeworkDraft,
    body: PublishSelection,
    *,
    require_confirmed: bool = True,
) -> PublishPreview:
    if draft.revision != body.expected_revision:
        raise ReviewError(409, "revision_conflict", "草稿已更新，请刷新后重新核对")
    if draft.status == "parsing" or draft.active_publication_id:
        raise ReviewError(409, "publish_in_progress", "草稿正在解析或发布，请稍后刷新")
    if len(body.record_ids) != len(set(body.record_ids)):
        raise ReviewError(422, "duplicate_records", "请选择不重复的作答记录")
    records = {r.record_id: r for r in draft.records}
    if not set(body.record_ids) <= records.keys():
        raise ReviewError(422, "record_not_found", "选择的作答不存在")
    questions = {q.question_id: q for q in draft.questions}
    roster = {s.id for _, s in members(session, draft.class_id)}
    problems = []
    selected = [records[rid] for rid in body.record_ids]
    pairs = set()
    for record in selected:

        def add(field, message, record_id=record.record_id):
            problems.append(ReviewProblem(record_id=record_id, field=field, message=message))

        if record.review_status == "published":
            raise ReviewError(409, "already_published", "所选记录已发布，请勿重复导入")
        if not draft.class_id:
            add("class_id", "请先将个人草稿绑定到班级")
        if record.identity.student_id not in roster:
            add("student_id", "请选择本班有效学生")
        if require_confirmed and (
            record.review_status != "confirmed" or record.identity.status != "confirmed"
        ):
            add("review_status", "请先标记该记录为已核对")
        if not record.student_answer or not record.student_answer.strip():
            add("student_answer", "请补充学生原始作答")
        question = questions.get(record.question_id)
        if question is None:
            add("question_id", "题目不存在")
            continue
        for field in ("stem", "correct_answer", "knowledge_point_id"):
            value = getattr(question, field)
            if not value or not value.strip():
                add(
                    field,
                    {
                        "stem": "请补全题干",
                        "correct_answer": "请补充参考答案",
                        "knowledge_point_id": "请选择主知识点",
                    }[field],
                )
        if question.knowledge_point_id and (
            question.knowledge_point_id == PENDING_KNOWLEDGE_POINT_ID
            or session.get(KnowledgePoint, question.knowledge_point_id) is None
        ):
            add("knowledge_point_id", "请选择目录中有效的主知识点")
        if question.item_id:
            item = accessible_item(session, draft.class_id, question.item_id)
            if item is None:
                add("item_id", "已有题目不存在或不属于当前可用范围；可清除关联后作为新题")
            elif (item.stem, item.answer, item.knowledge_point_id) != (
                question.stem,
                question.correct_answer,
                question.knowledge_point_id,
            ):
                add("item_id", "修改内容与已有题目不一致，请清除关联后作为新题发布")
        for issue in question.issues + record.issues:
            if issue.blocking and issue.resolution is None:
                add(issue.field, issue.message)
        refs = question.source_refs + record.source_refs
        source_ids = {s.source_id for s in draft.sources}
        if not refs or any(ref.source_id not in source_ids for ref in refs):
            add("source_refs", "缺少可追溯的原始材料")
        pair = (record.identity.student_id, item_id_for(draft, question))
        if pair in pairs:
            add("student_id", "本次发布中同一学生同一道题只能有一条作答")
        pairs.add(pair)
    return PublishPreview(
        draft_id=draft.draft_id,
        revision=draft.revision,
        selected_records=len(selected),
        students=len({r.identity.student_id for r in selected if r.identity.student_id}),
        items=len(
            {
                item_id_for(draft, questions[r.question_id])
                for r in selected
                if r.question_id in questions
            }
        ),
        answers=len(selected),
        remaining_records=sum(
            r.record_id not in body.record_ids and r.review_status not in ("published", "skipped")
            for r in draft.records
        ),
        blocking_issues=problems,
        can_publish=not problems,
    )


def require_complete(preview: PublishPreview) -> None:
    if not preview.can_publish:
        raise ReviewError(
            422,
            "review_incomplete",
            "请先处理待确认项，再核对或发布",
            issues=[p.model_dump() for p in preview.blocking_issues],
        )


def update_draft(session: Session, principal, row: DraftRow, body: DraftUpdate) -> HomeworkDraft:
    draft = HomeworkDraft.model_validate(row.payload)
    if (
        draft.revision != body.expected_revision
        or draft.status == "parsing"
        or draft.active_publication_id
    ):
        raise ReviewError(409, "revision_conflict", "草稿正在处理或已更新，请刷新后重试")
    actor = principal.teacher_id
    now = datetime.now(UTC)

    def change(target, target_id, field, value, note=None):
        before = getattr(target, field)
        if before != value:
            draft.edits.append(
                Edit(
                    revision=draft.revision + 1,
                    target_id=target_id,
                    field=field,
                    before=before,
                    after=value,
                    by_teacher_id=actor,
                    at=now,
                    note=note,
                )
            )
            setattr(target, field, value)
            return True
        return False

    if "class_id" in body.model_fields_set and body.class_id != draft.class_id:
        if draft.class_id is not None or body.class_id is None:
            raise ReviewError(409, "class_locked", "已绑定的班级不能切换")
        check_class(session, principal, body.class_id)
        change(draft, draft.draft_id, "class_id", body.class_id)
        draft.created_by_teacher_id = draft.created_by_teacher_id or actor
        propose_identities(session, draft)
    questions = {q.question_id: q for q in draft.questions}
    records = {r.record_id: r for r in draft.records}
    frozen = {r.question_id for r in draft.records if r.review_status == "published"}
    changed_fields = set()
    for update in body.question_updates:
        question = questions.get(update.question_id)
        if question is None:
            raise ReviewError(422, "question_not_found", "题目不存在")
        for field in update.model_fields_set - {"question_id"}:
            value = getattr(update, field)
            value = value.strip() or None if isinstance(value, str) else value
            if value == getattr(question, field):
                continue
            if question.question_id in frozen:
                raise ReviewError(409, "already_published", "已发布题目不可修改，请另建作业草稿")
            change(question, question.question_id, field, value)
            changed_fields.add((question.question_id, field))
            for record in draft.records:
                if (
                    record.question_id == question.question_id
                    and record.review_status == "confirmed"
                ):
                    change(record, record.record_id, "review_status", "pending")
        if question.item_id and draft.class_id:
            item = accessible_item(session, draft.class_id, question.item_id)
            if item:
                for field, value in (
                    ("stem", item.stem),
                    ("correct_answer", item.answer),
                    ("knowledge_point_id", item.knowledge_point_id),
                ):
                    if getattr(question, field) is None:
                        change(question, question.question_id, field, value)
    to_confirm = []
    roster = {s.id for _, s in members(session, draft.class_id)}
    for update in body.record_updates:
        record = records.get(update.record_id)
        if record is None:
            raise ReviewError(422, "record_not_found", "作答不存在")
        if record.review_status == "published":
            raise ReviewError(409, "already_published", "已发布作答不可修改")
        for field in ("student_id", "student_answer"):
            if field not in update.model_fields_set:
                continue
            value = getattr(update, field)
            if field == "student_id":
                if value is not None and value not in roster:
                    raise ReviewError(422, "student_not_in_class", "请选择本班有效学生")
                target = record.identity
            else:
                target = record
            if change(target, record.record_id, field, value, update.note):
                changed_fields.add(
                    (record.record_id, "identity" if field == "student_id" else field)
                )
                change(record, record.record_id, "review_status", "pending")
                record.identity.status = "proposed" if record.identity.student_id else "unmatched"
        if update.review_status is not None:
            change(record, record.record_id, "review_status", update.review_status, update.note)
            if update.review_status == "confirmed":
                record.identity.status = "confirmed"
                to_confirm.append(record.record_id)
    for target in draft.questions + draft.records:
        if (
            hasattr(target, "stem")
            and target.question_id in frozen
            or getattr(target, "review_status", None) == "published"
        ):
            continue
        tid = target.question_id if hasattr(target, "stem") else target.record_id
        for issue in target.issues:
            value_present = (
                bool(getattr(target, issue.field, None))
                if issue.field != "identity"
                else target.identity.status == "confirmed"
            )
            if (
                issue.code in ("missing_field", "answer_missing", "identity_unmatched")
                and value_present
                or (tid, issue.field) in changed_fields
                and value_present
            ):
                issue.resolution = IssueResolution(action="corrected", by_teacher_id=actor, at=now)
    issue_targets = {i.issue_id: (t, i) for t in draft.questions + draft.records for i in t.issues}
    for resolution in body.issue_resolutions:
        entry = issue_targets.get(resolution.issue_id)
        if entry is None:
            raise ReviewError(422, "issue_not_found", "疑点不存在")
        target, issue = entry
        if (
            hasattr(target, "stem")
            and target.question_id in frozen
            or getattr(target, "review_status", None) == "published"
        ):
            raise ReviewError(409, "already_published", "已发布内容不可修改")
        old = issue.resolution.model_dump(mode="json") if issue.resolution else None
        issue.resolution = IssueResolution(
            action=resolution.action, by_teacher_id=actor, at=now, note=resolution.note
        )
        draft.edits.append(
            Edit(
                revision=draft.revision + 1,
                target_id=resolution.issue_id,
                field="resolution",
                before=old,
                after=issue.resolution.model_dump(mode="json"),
                by_teacher_id=actor,
                at=now,
                note=resolution.note,
            )
        )
    if to_confirm:
        require_complete(
            inspect_selection(
                session,
                draft,
                PublishSelection(expected_revision=draft.revision, record_ids=to_confirm),
            )
        )
    draft.revision += 1
    draft.updated_at = now
    draft.status = (
        "published"
        if any(r.review_status == "published" for r in draft.records)
        and all(r.review_status in ("published", "skipped") for r in draft.records)
        else ("review" if draft.questions else "parse_failed")
    )
    row.payload = draft.model_dump(mode="json")
    session.add(row)
    return draft


def confirmed_rows(draft: HomeworkDraft, record_ids: list[str]) -> list[HomeworkRow]:
    questions = {q.question_id: q for q in draft.questions}
    records = {r.record_id: r for r in draft.records}
    rows = []
    for rid in record_ids:
        record = records[rid]
        question = questions[record.question_id]
        rows.append(
            HomeworkRow(
                student_id=record.identity.student_id,
                item_id=item_id_for(draft, question),
                stem=question.stem,
                correct_answer=question.correct_answer,
                knowledge_point_id=question.knowledge_point_id,
                student_answer=record.student_answer,
            )
        )
    return rows
