"""Student questions use the regular task checkpoint, diagnosis and planning queues."""

from datetime import date
from uuid import uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

from app.classroom import StudentPlacement, active_class, write_lock
from app.contracts.api import ChatTurn, QuestionCreate
from app.contracts.entities import Evidence, Item, KnowledgePoint, Student, Task
from app.contracts.enums import (
    EvidenceKind,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    TaskStatus,
    TaskType,
)
from app.contracts.values import PENDING_KNOWLEDGE_POINT_ID, PENDING_KNOWLEDGE_POINT_NAME
from app.orchestration.storage import (
    ItemExposure,
    NodeReceipt,
    bind_question_photos,
    queue_work,
    record_exposure,
    task_question_photo_urls,
)
from app.verify.tools import stem_fingerprint

SOLUTION_LEVEL = 4
AFTER_SOLUTION = "有哪一步不清楚可以继续问我；觉得理解了就告诉我，我们用一道新题检查一下。"
READY_MESSAGE = "好的，接下来用一道没见过的同类题独立检查一下。"


def _reject_open_verification(session, student_id, stem, fingerprint: str | None = None):
    answered = (
        select(Evidence.id)
        .where(
            Evidence.task_id == ItemExposure.task_id,
            Evidence.item_id == ItemExposure.item_id,
            Evidence.kind == EvidenceKind.VERIFICATION,
        )
        .exists()
    )
    items = list(
        session.exec(
            select(Item)
            .join(ItemExposure, ItemExposure.item_id == Item.id)
            .join(Task, Task.id == ItemExposure.task_id)
            .where(
                ItemExposure.student_id == student_id,
                Item.purpose == ItemPurpose.VERIFICATION,
                Task.status.in_((TaskStatus.PENDING, TaskStatus.ACTIVE)),
                ~answered,
            )
        )
    )
    compact_stem = "".join(stem.split())
    for item in items:
        if "".join(item.stem.split()) == compact_stem:
            raise HTTPException(409, "这会给出一道尚未作答的独立验证题的结果，请先完成作答")
        if (
            fingerprint is not None
            and item.fingerprint is not None
            and item.fingerprint == fingerprint
        ):
            raise HTTPException(409, "这会给出一道尚未作答的独立验证题的结果，请先完成作答")


def _ensure_pending_point(session: Session) -> None:
    if session.get(KnowledgePoint, PENDING_KNOWLEDGE_POINT_ID) is None:
        session.merge(
            KnowledgePoint(
                id=PENDING_KNOWLEDGE_POINT_ID,
                lecture=0,
                section=0,
                name=PENDING_KNOWLEDGE_POINT_NAME,
            )
        )
        session.flush()


def _item_fingerprint(runtime, item: Item) -> str:
    if item.spec is not None:
        return runtime.questions.tools.fingerprint(item.spec)
    return stem_fingerprint(item.stem)


def _draft_fingerprint(runtime, draft, stem: str) -> str:
    if draft.spec is not None:
        return runtime.questions.tools.fingerprint(draft.spec)
    return stem_fingerprint(stem)


async def create_question(
    runtime, student_id: str, body: QuestionCreate, *, teacher_id: str | None = None
) -> Task:
    content, attempt = body.content.strip(), body.attempt.strip()
    if max(len(content), len(attempt)) > 8000:
        raise HTTPException(422, "题目和尝试均不得超过 8000 字")
    if not content and not body.photo_ids:
        raise HTTPException(422, "请提供题目文字或题目图片")
    with Session(runtime.engine) as session:
        if session.get(Student, student_id) is None:
            raise HTTPException(404, "学生不存在")
        if content:
            _reject_open_verification(session, student_id, content)
    runtime._require("questions", "tutor", "planner", "verifier", "knowledge")
    with Session(runtime.engine, expire_on_commit=False) as session:
        write_lock(session)
        class_id = active_class(session, student_id)
        placement = session.get(StudentPlacement, student_id)
        if teacher_id is not None and (
            placement is None or placement.teacher_id != teacher_id or class_id is None
        ):
            raise HTTPException(404, "学生不存在")
        today = runtime.task_date_for_class(class_id) or date.today()
        _ensure_pending_point(session)
        task = Task(
            type=TaskType.QUESTION,
            class_id=class_id,
            student_id=student_id,
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            reason="学生主动提问",
            created_on=today,
        )
        session.add(task)
        session.flush()
        item = Item(
            id=f"student-question.{uuid4().hex}",
            class_id=class_id,
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            purpose=ItemPurpose.PRACTICE,
            source=ItemSource.GENERATED,
            stem=content or "（题目见上传图片）",
            spec=None,
            answer="",
        )
        session.add(item)
        session.flush()
        try:
            bind_question_photos(session, student_id, task.id, body.photo_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if content:
            # The flush holds SQLite's writer lock while the final anti-screenshot check runs.
            _reject_open_verification(session, student_id, content, item.fingerprint)
        session.add(
            NodeReceipt(
                key=f"question:{task.id}",
                result={
                    "item": item.id,
                    "content": content,
                    "attempt": attempt,
                    "photo_ids": list(body.photo_ids),
                    "interpreted": False,
                    "revision": 0,
                    "clarifications": [],
                },
            )
        )
        queue_work(session, f"task:{task.id}", {"teaching_date": today.isoformat()})
        session.commit()
    await runtime._publish_task(task)
    runtime.recover_pending()
    return task


async def _interpret(runtime, state, task_id: int) -> tuple[Item, list[str], str | None]:
    """Read the saved question; classify and solve it once, preserving the input on failure."""
    with Session(runtime.engine) as session:
        task = runtime._task(session, task_id)
        receipt = session.get(NodeReceipt, f"question:{task_id}")
        initial = dict(receipt.result)
        item = session.get(Item, initial["item"])
        student_id = task.student_id
        content = initial.get("content", "")
        history = state.get("messages", [])
        images = task_question_photo_urls(session, student_id, task_id)
        interpreted = initial.get("interpreted", False)
        clarifications = list(initial.get("clarifications", []))
        blocked = initial.get("blocked")
        session.expunge(item)
    if interpreted:
        return item, clarifications, blocked
    draft = await runtime.questions.interpret(content, images, history)
    with Session(runtime.engine, expire_on_commit=False) as session:
        task = runtime.working_task(session, task_id)
        receipt = session.get(NodeReceipt, f"question:{task_id}")
        initial = dict(receipt.result)
        item = session.get(Item, initial["item"])
        item.stem = draft.stem or item.stem
        item.knowledge_point_id = draft.knowledge_point_id or PENDING_KNOWLEDGE_POINT_ID
        item.spec = draft.spec
        item.answer = draft.answer
        item.rubric = draft.rubric
        item.fingerprint = _item_fingerprint(runtime, item)
        task.knowledge_point_id = item.knowledge_point_id
        blocked = None
        try:
            _reject_open_verification(session, task.student_id, item.stem, item.fingerprint)
        except HTTPException as exc:
            # The interpreted problem is the same as (or an equivalent rewrite of) an
            # unanswered verification item. Keep the input but do not tutor it.
            blocked = str(exc.detail)
        initial["interpreted"] = True
        initial["clarifications"] = list(draft.clarifications)
        initial["blocked"] = blocked
        receipt.result = initial
        session.add(receipt)
        intake_key = f"question:{task_id}:intake"
        if blocked is None and session.get(NodeReceipt, intake_key) is None:
            evidence = Evidence(
                student_id=task.student_id,
                task_id=task.id,
                item_id=item.id,
                knowledge_point_id=task.knowledge_point_id,
                kind=EvidenceKind.PRACTICE,
                student_answer=initial.get("attempt", ""),
                judged_by=JudgedBy.MODEL,
                reason=(
                    "学生主动提问（题目结构已用工具核验）："
                    if item.spec is not None
                    else "学生主动提问（参考解由模型理解，未用工具核验）："
                )
                + (initial.get("content") or "（题目见上传图片）"),
                teaching_date=date.fromisoformat(state["teaching_date"]),
            )
            session.add(evidence)
            session.flush()
            task.evidence_ids = [*task.evidence_ids, evidence.id]
            session.add(NodeReceipt(key=intake_key, result={"evidence_id": evidence.id}))
        if blocked is None:
            record_exposure(session, task.student_id, task.id, item)
        session.commit()
        session.refresh(item)
        session.expunge(item)
        return item, list(draft.clarifications), blocked


async def question_turn(runtime, state) -> dict:
    task_id = state["task_id"]
    incoming = state.get("incoming_message")
    item, clarifications, blocked = await _interpret(runtime, state, task_id)
    with Session(runtime.engine) as session:
        task = runtime._task(session, task_id)
        images = task_question_photo_urls(session, task.student_id, task_id)
        receipt = session.get(NodeReceipt, f"question:{task_id}")
        initial = dict(receipt.result)
        attempt = initial.get("attempt", "")
    history = state["messages"]
    first_turn = not history
    if first_turn:
        history = [ChatTurn(role="user", content=item.stem).model_dump(mode="json")]
        if attempt:
            history.append(ChatTurn(role="user", content=attempt).model_dump(mode="json"))
    if blocked:
        return _reply(history, blocked, finished=True)
    if first_turn and not attempt:
        # Let the model decide how to open: explain a concept directly, or frame the
        # problem and invite an attempt. Clarifications from interpretation stay visible.
        text = await runtime.questions.discuss(
            item, item.stem, "opening", images=images, history=history
        )
        prompt = " ".join([*clarifications, text]) if clarifications else text
        return _reply(history, prompt, item_id=item.id)

    level = state.get("question_hint", 0)
    solution_shown = level >= SOLUTION_LEVEL
    # Leaving the DiagnosticTutor resumes work on the student's original problem.
    if state["phase"] == "question_diagnosis" and incoming is None:
        attempt = state.get("question_attempt", "")
        observation = state.get("question_observation", "")
        confirmed = state.get("tutor_turn", {}).get("confirmed_misconception_ids", [])
        return await _next_hint(
            runtime, state, item, history, attempt, observation, confirmed, level
        )

    message = incoming["content"] if incoming else attempt
    last_hint = next(
        (message["content"] for message in reversed(history) if message["role"] == "assistant"),
        "",
    )
    review = await runtime.questions.review(
        item, message, images=images, history=history, level=level, last_hint=last_hint
    )

    if review.intent == "correction":
        return await _correct(runtime, state, item, history, message, review, images)

    if review.intent == "ready":
        # Saying "I get it" is not evidence. Close the question so the planner schedules
        # an unseen independent verification item.
        return _reply(history, READY_MESSAGE, finished=True)

    if solution_shown and review.intent != "attempt":
        text = await runtime.questions.discuss(
            item,
            message,
            review.intent,
            images=images,
            history=history,
            level=level,
            last_hint=last_hint,
            solution_shown=True,
        )
        return _reply(history, text, item_id=item.id)

    if review.intent == "hint_request":
        return await _next_hint(
            runtime,
            state,
            item,
            history,
            message,
            review.observation,
            [],
            level,
        )

    if review.intent != "attempt":
        text = await runtime.questions.discuss(
            item,
            message,
            review.intent,
            images=images,
            history=history,
            level=level,
            last_hint=last_hint,
        )
        return _reply(history, text, item_id=item.id)

    if not message.strip() and review.is_correct is None:
        text = await runtime.questions.discuss(
            item, message, "other", images=images, history=history, level=level, last_hint=last_hint
        )
        return _reply(history, text, item_id=item.id)

    check, judged_by = _decide(runtime, item, review)
    if check is True:
        _record_attempt(
            runtime,
            state,
            item,
            review.final_answer or message,
            correct=True,
            judged_by=judged_by,
        )
        return _reply(history, "你的结果通过了核验。接下来可以做一道同类新题。", finished=True)

    if solution_shown:
        # After the full solution, a wrong attempt gets direct feedback instead of a
        # fresh diagnosis; the attempt is still recorded as evidence.
        if check is not None:
            _record_attempt(
                runtime,
                state,
                item,
                review.final_answer or message,
                correct=check,
                judged_by=judged_by,
                observation=review.observation,
            )
        text = await runtime.questions.discuss(
            item,
            message,
            "other",
            images=images,
            history=history,
            level=level,
            last_hint=last_hint,
            solution_shown=True,
        )
        return _reply(history, text, item_id=item.id)

    should_diagnose = (
        item.spec is not None
        and item.knowledge_point_id != PENDING_KNOWLEDGE_POINT_ID
        and (check is False or review.stuck_step or review.first_error_step)
    )
    if should_diagnose:
        evidence_id = _record_attempt(
            runtime,
            state,
            item,
            review.final_answer if check is False and review.final_answer else message,
            correct=False if check is False else None,
            judged_by=judged_by,
            observation=(
                f"中间步骤待诊断：{review.first_error_step}；{review.observation}"
                if review.first_error_step
                else ""
            ),
        )
        return {
            "question_attempt": message,
            "question_observation": review.observation,
            "question_diagnosed": True,
            "question_diagnostic_evidence_id": evidence_id,
            "phase": "question_diagnosis",
            "incoming_message": None,
            "messages": history,
        }

    if not state.get("question_clarified") and not state.get("question_diagnosed"):
        prompt = await runtime.questions.guide(
            item,
            message,
            review.observation,
            [],
            0,
            images=images,
            history=history,
            last_hint=last_hint,
        )
        return {
            **_reply(history, prompt, item_id=item.id),
            "question_attempt": message,
            "question_observation": review.observation,
            "question_clarified": True,
        }

    if check is not None or review.first_error_step or review.stuck_step:
        _record_attempt(
            runtime,
            state,
            item,
            review.final_answer or message,
            correct=False if check is False else None,
            judged_by=judged_by,
            observation=review.observation,
        )
    if review.hint_advance:
        return await _next_hint(
            runtime, state, item, history, message, review.observation, [], level
        )
    text = await runtime.questions.discuss(
        item, message, "other", images=images, history=history, level=level, last_hint=last_hint
    )
    return _reply(history, text, item_id=item.id)


def _decide(runtime, item: Item, review) -> tuple[bool | None, JudgedBy]:
    if review.final_answer:
        if item.spec is not None:
            result = runtime.questions.tools.check_equivalent(
                review.final_answer, item.answer, item.spec.answer_mode, item.spec.variables
            )
            if result.equivalent is not None:
                return result.equivalent, JudgedBy.TOOL
        if review.is_correct is not None:
            return review.is_correct, JudgedBy.MODEL
    elif item.spec is None and review.is_correct is not None:
        return review.is_correct, JudgedBy.MODEL
    return None, JudgedBy.MODEL


async def _next_hint(runtime, state, item, history, attempt, observation, confirmed, level):
    next_level = level + 1
    last_hint = next(
        (message["content"] for message in reversed(history) if message["role"] == "assistant"),
        "",
    )
    with Session(runtime.engine) as session:
        task = runtime._task(session, state["task_id"])
        images = task_question_photo_urls(session, task.student_id, state["task_id"])
    text = await runtime.questions.guide(
        item,
        attempt,
        observation,
        confirmed,
        next_level,
        images=images,
        history=history,
        last_hint=last_hint,
    )
    if next_level >= SOLUTION_LEVEL:
        label = (
            f"核验结果：${item.answer}$"
            if item.spec is not None and item.answer
            else f"参考结论（模型生成，未用工具核验）：{item.answer}"
            if item.answer
            else ""
        )
        content = "\n\n".join(part for part in (text, label, AFTER_SOLUTION) if part)
        # Keep the conversation open: the student can still ask about the solution.
        return {
            **_reply(history, content, solution=True, item_id=item.id),
            "question_hint": next_level,
        }
    return {
        **_reply(history, text, hint=next_level, item_id=item.id),
        "question_hint": next_level,
    }


async def _correct(runtime, state, item, history, message, review, images) -> dict:
    draft = await runtime.questions.interpret(message, images, history)
    stem = draft.stem or item.stem
    # A correction must not turn an unanswered verification item into a tutored question:
    # the level-4 hint would then reveal that item's answer.
    with Session(runtime.engine) as session:
        task = runtime._task(session, state["task_id"])
        try:
            _reject_open_verification(
                session, task.student_id, stem, _draft_fingerprint(runtime, draft, stem)
            )
        except HTTPException as exc:
            return _reply(history, str(exc.detail), item_id=item.id)
    with Session(runtime.engine, expire_on_commit=False) as session:
        task = runtime.working_task(session, state["task_id"])
        receipt = session.get(NodeReceipt, f"question:{state['task_id']}")
        initial = dict(receipt.result)
        item = session.get(Item, initial["item"])
        # The old attempts and diagnoses describe the previous wording. Evidence rows stay
        # append-only for history, but they no longer belong to the task's active chain.
        superseded = list(task.evidence_ids)
        task.evidence_ids = []
        initial["superseded_evidence_ids"] = list(
            dict.fromkeys([*initial.get("superseded_evidence_ids", []), *superseded])
        )
        initial["corrections"] = [
            *initial.get("corrections", []),
            {
                "revision": initial.get("revision", 0),
                "previous_stem": item.stem,
                "previous_answer": item.answer,
                "reason": review.reason,
            },
        ]
        item.stem = stem
        item.knowledge_point_id = draft.knowledge_point_id or item.knowledge_point_id
        item.spec = draft.spec
        item.answer = draft.answer
        item.rubric = draft.rubric
        item.fingerprint = _item_fingerprint(runtime, item)
        task.knowledge_point_id = item.knowledge_point_id
        initial["revision"] = initial.get("revision", 0) + 1
        initial["clarifications"] = list(draft.clarifications)
        receipt.result = initial
        session.add(receipt)
        record_exposure(session, task.student_id, task.id, item)
        session.commit()
        session.refresh(item)
        session.expunge(item)
    text = await runtime.questions.discuss(
        item,
        message,
        "correction",
        images=images,
        history=history,
        level=0,
        last_hint="",
    )
    return {
        **_reply(history, text, item_id=item.id),
        "question_hint": 0,
        "question_attempt": "",
        "question_observation": "",
        "question_clarified": False,
        "question_diagnosed": False,
        "question_diagnostic_evidence_id": None,
    }


def _record_attempt(runtime, state, item, answer, *, correct, judged_by, observation=""):
    with Session(runtime.engine) as session:
        task = runtime.working_task(session, state["task_id"])
        revision = _revision(session, task.id)
        suffix = {True: "correct_attempt", False: "first_attempt", None: "stuck_attempt"}[correct]
        key = f"question:{task.id}:{item.id}:{revision}:{suffix}"
        if receipt := session.get(NodeReceipt, key):
            return receipt.result["evidence_id"]
        evidence = Evidence(
            student_id=task.student_id,
            class_id=task.class_id,
            knowledge_point_id=task.knowledge_point_id,
            task_id=task.id,
            item_id=item.id,
            kind=EvidenceKind.PRACTICE,
            student_answer=answer,
            correct_answer=item.answer,
            is_correct=correct,
            judged_by=judged_by,
            reason=observation
            or {
                True: "主动提问的作答通过核验"
                if judged_by == JudgedBy.TOOL
                else "主动提问的作答由模型按要点判定为正确",
                False: "主动提问的首次错答未通过工具核验"
                if judged_by == JudgedBy.TOOL
                else "主动提问的首次错答由模型按要点判定为错误",
                None: "学生指出具体卡点或模型暂时无法判定",
            }[correct],
            teaching_date=date.fromisoformat(state["teaching_date"]),
        )
        session.add(evidence)
        session.flush()
        task.evidence_ids = [*task.evidence_ids, evidence.id]
        evidence_id = evidence.id
        session.add(NodeReceipt(key=key, result={"evidence_id": evidence_id}))
        session.commit()
        return evidence_id


def _revision(session: Session, task_id: int) -> int:
    receipt = session.get(NodeReceipt, f"question:{task_id}")
    return int(receipt.result.get("revision", 0)) if receipt else 0


def _reply(history, content, *, hint=None, finished=False, solution=False, item_id=None):
    message = ChatTurn(role="assistant", content=content, hint_level=hint, solution=solution)
    return {
        "phase": "question_finished" if finished else "question",
        "messages": [*history, message.model_dump(mode="json")],
        "awaiting_item_id": item_id,
        "incoming_message": None,
    }
