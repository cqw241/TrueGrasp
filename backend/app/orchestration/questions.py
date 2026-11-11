"""Student questions use the regular task checkpoint, diagnosis and planning queues."""

from datetime import date
from uuid import uuid4

from fastapi import HTTPException
from openai import OpenAIError
from sqlmodel import Session, select

from app.contracts.api import ChatTurn, QuestionCreate
from app.contracts.entities import Evidence, Item, Student, Task
from app.contracts.enums import (
    EvidenceKind,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    TaskStatus,
    TaskType,
)
from app.contracts.events import EvidenceAdded
from app.llm.client import LLMOutputError
from app.orchestration.storage import ItemExposure, NodeReceipt, queue_work, record_exposure
from app.verify.tools import SympyMathTools


def _reject_open_verification(session, student_id, stem, question=None):
    answered = (
        select(Evidence.id)
        .where(
            Evidence.task_id == ItemExposure.task_id,
            Evidence.item_id == ItemExposure.item_id,
            Evidence.kind == EvidenceKind.VERIFICATION,
        )
        .exists()
    )
    items = session.exec(
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
    compact_stem = "".join(stem.split())
    tools = SympyMathTools()
    for item in items:
        if "".join(item.stem.split()) == compact_stem or (
            question is not None
            and (
                item.fingerprint == question.fingerprint
                or (
                    item.spec is not None
                    and tools.same_revealed_answer(
                        question.answer, question.spec, item.answer, item.spec
                    )
                )
            )
        ):
            raise HTTPException(409, "这会给出一道尚未作答的独立验证题的结果，请先完成作答")


async def create_question(runtime, student_id: str, body: QuestionCreate) -> Task:
    content, attempt = body.content.strip(), body.attempt.strip()
    if not content or max(len(content), len(attempt)) > 8000:
        raise HTTPException(422, "请提供题目，题目和尝试均不得超过 8000 字")
    with Session(runtime.engine) as session:
        if session.get(Student, student_id) is None:
            raise HTTPException(404, "学生不存在")
        _reject_open_verification(session, student_id, content)
    runtime._require("questions", "tutor", "planner", "verifier", "knowledge")
    try:
        draft = await runtime.questions.solve(content)
    except (OpenAIError, LLMOutputError) as exc:
        raise HTTPException(502, "暂时无法核验题目，请重试") from exc
    if draft is None:
        raise HTTPException(422, "无法独立核验这道题，请补全题目条件或输入可计算的高数题")

    today = runtime.teaching_date() if runtime.teaching_date else date.today()
    with Session(runtime.engine, expire_on_commit=False) as session:
        item = Item(
            id=f"student-question.{uuid4().hex}",
            knowledge_point_id=draft.knowledge_point_id,
            purpose=ItemPurpose.PRACTICE,
            source=ItemSource.GENERATED,
            stem=content,
            spec=draft.spec,
            answer=draft.answer,
            fingerprint=runtime.questions.tools.fingerprint(draft.spec),
        )
        task = Task(
            type=TaskType.QUESTION,
            student_id=student_id,
            knowledge_point_id=item.knowledge_point_id,
            reason="学生主动提问",
            created_on=today,
        )
        session.add(item)
        session.add(task)
        session.flush()
        # The flush holds SQLite's writer lock. No verification can claim this
        # fingerprint between the recheck and the new question's exposure.
        _reject_open_verification(session, student_id, content, item)
        evidence = Evidence(
            student_id=student_id,
            task_id=task.id,
            item_id=item.id,
            knowledge_point_id=task.knowledge_point_id,
            kind=EvidenceKind.PRACTICE,
            student_answer=attempt,
            judged_by=JudgedBy.TOOL,
            reason=f"学生主动提问（题目已独立核验）：{content}",
            teaching_date=today,
        )
        session.add(evidence)
        session.flush()
        task.evidence_ids = [evidence.id]
        record_exposure(session, student_id, task.id, item)
        session.add(
            NodeReceipt(key=f"question:{task.id}", result={"item": item.id, "attempt": attempt})
        )
        queue_work(session, f"task:{task.id}", {"teaching_date": today.isoformat()})
        session.commit()
    await runtime._publish_task(task)
    await runtime.bus.publish(
        EvidenceAdded(
            student_id=student_id,
            evidence_id=evidence.id,
            knowledge_point_id=task.knowledge_point_id,
        )
    )
    runtime.recover_pending()
    return task


async def question_turn(runtime, state) -> dict:
    task_id = state["task_id"]
    history = state["messages"]
    incoming = state.get("incoming_message")
    with Session(runtime.engine) as session:
        initial = session.get(NodeReceipt, f"question:{task_id}").result
        item = session.get(Item, initial["item"])
        confirmed_ids = state.get("tutor_turn", {}).get("confirmed_misconception_ids", [])
        session.expunge(item)
    if not history:
        history = [ChatTurn(role="user", content=item.stem).model_dump(mode="json")]
        if not initial["attempt"]:
            return _reply(history, "请先写下你的尝试，或说说你卡在哪一步。", item_id=item.id)
        attempt = initial["attempt"]
        history.append(ChatTurn(role="user", content=attempt).model_dump(mode="json"))
    else:
        attempt = incoming["content"] if incoming else state.get("question_attempt", "")

    level = state.get("question_hint", 0)
    # Leaving the T09 probe loop resumes work on the student's original problem.
    if state["phase"] == "question_diagnosis":
        observation = state["question_observation"]
    else:
        review = await runtime.questions.review(item, attempt)
        observation = review.observation
        check = None
        if review.final_answer:
            check = runtime.questions.tools.check_equivalent(
                review.final_answer, item.answer, item.spec.answer_mode, item.spec.variables
            ).equivalent
        if check is True:
            _record_attempt(runtime, state, item, attempt, correct=True)
            return _reply(history, "你的结果通过了核验。接下来可以做一道同类新题。", finished=True)
        checkpoint = {"question_attempt": attempt, "question_observation": observation}
        if (check is False or review.stuck_step or review.first_error_step) and not state.get(
            "question_diagnosed"
        ):
            evidence_id = _record_attempt(
                runtime,
                state,
                item,
                review.final_answer if check is False else attempt,
                correct=False if check is False else None,
                observation=(
                    f"中间步骤待诊断：{review.first_error_step}；{observation}"
                    if review.first_error_step
                    else ""
                ),
            )
            return {
                **checkpoint,
                "question_diagnosed": True,
                "question_diagnostic_evidence_id": evidence_id,
                "phase": "question_diagnosis",
                "incoming_message": None,
                "messages": history,
            }
        if not state.get("question_clarified") and not state.get("question_diagnosed"):
            prompt = await runtime.questions.guide(item, attempt, observation, [], 0)
            return {
                **checkpoint,
                "question_clarified": True,
                **_reply(history, prompt, item_id=item.id),
            }

    next_level = level + 1
    text = await runtime.questions.guide(item, attempt, observation, confirmed_ids, next_level)
    if next_level == 4:
        return _reply(
            history, f"{text}\n\n核验结果：${item.answer}$。", finished=True, solution=True
        )
    return {
        **_reply(history, text, hint=next_level, item_id=item.id),
        "question_hint": next_level,
    }


def _record_attempt(runtime, state, item, answer, *, correct, observation=""):
    with Session(runtime.engine) as session:
        task = runtime._task(session, state["task_id"])
        suffix = {True: "correct_attempt", False: "first_attempt", None: "stuck_attempt"}[correct]
        key = f"question:{task.id}:{suffix}"
        if receipt := session.get(NodeReceipt, key):
            return receipt.result["evidence_id"]
        evidence = Evidence(
            student_id=task.student_id,
            knowledge_point_id=task.knowledge_point_id,
            task_id=task.id,
            item_id=item.id,
            kind=EvidenceKind.PRACTICE,
            student_answer=answer,
            is_correct=correct,
            judged_by=JudgedBy.MODEL if correct is None else JudgedBy.TOOL,
            reason=observation
            or {
                True: "主动提问的作答通过核验",
                False: "主动提问的首次错答与核验结果不等价",
                None: "学生指出具体卡点，启动诊断",
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


def _reply(history, content, *, hint=None, finished=False, solution=False, item_id=None):
    message = ChatTurn(role="assistant", content=content, hint_level=hint, solution=solution)
    return {
        "phase": "question_finished" if finished else "question",
        "messages": [*history, message.model_dump(mode="json")],
        "awaiting_item_id": item_id,
        "incoming_message": None,
    }
