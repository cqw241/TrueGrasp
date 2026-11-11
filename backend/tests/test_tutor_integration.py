"""Real tutor, real verifier and ledger across the public orchestration seam."""

import json
from datetime import timedelta
from io import BytesIO

import pytest
from httpx2 import ASGITransport, AsyncClient
from PIL import Image
from sqlmodel import Session, select

from app.agents.planner import create_planner
from app.agents.tutor import create_tutor
from app.agents.tutor.models import CandidateSelection
from app.agents.tutor.models import QuestionDraft as ProbeDraft
from app.agents.tutor.queries import diagnosis
from app.agents.tutor.student_question import (
    AttemptReview,
    Guidance,
    HintReview,
    QuestionDraft,
    QuestionInterpretation,
    StudentQuestions,
)
from app.agents.verifier import create_verifier
from app.agents.verifier.agent import ItemDraft, ItemReview
from app.auth import authorize
from app.config import Settings
from app.contracts.agents import PlannerOutput
from app.contracts.api import MessageCreate, QuestionCreate
from app.contracts.entities import Event, Evidence, Mastery, Task
from app.contracts.enums import EvidenceKind, MasteryState, TaskType
from app.db import make_engine
from app.llm.vision import Transcription
from app.main import create_app
from app.orchestration import EventBus, Orchestrator
from app.orchestration.questions import create_question
from app.orchestration.storage import NodeReceipt, PendingWork
from app.verify.tools import SympyMathTools
from tests.test_planner import PlanningModel
from tests.test_tutor import FORMULA, MISS, POINT, TODAY, Model, Publisher, input_for
from tests.test_tutor import setup as setup

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Planner:
    async def plan(self, inp):
        return PlannerOutput()


class CombinedModel(Model):
    async def generate(self, messages, output, tools=None):
        if output is ItemReview:
            return ItemReview(valid=True, reason="符合目标维度及结构")
        if output is ItemDraft:
            payload = json.loads(messages[-1].content)
            dimension = payload["dimension"]
            seeds = {
                "correctness": ("sin(2*x)**3", r"6\sin^2(2x)\cos(2x)", []),
                "distractor": (
                    "cos(x**2)**2",
                    r"-4x\cos(x^2)\sin(x^2)",
                    [
                        {"misconception_id": m["id"], "answer": r"-2\cos(x^2)\sin(x^2)"}
                        for m in payload["input"]["confirmed_misconceptions"]
                    ],
                ),
                "near_transfer": (
                    "exp(sqrt(1+t**2))",
                    r"\frac{te^{\sqrt{1+t^2}}}{\sqrt{1+t^2}}",
                    [],
                ),
            }
            expr, answer, predictions = seeds[dimension]
            return ItemDraft(
                stem=f"求导：{expr}",
                spec={
                    "operation": "derivative",
                    "expr": expr,
                    "variables": ["t"] if dimension == "near_transfer" else ["x"],
                },
                answer=answer,
                predictions=predictions,
            )
        return await super().generate(messages, output, tools)


@pytest.mark.parametrize("initial", ["", "cos(x)", "不会用链式法则", "会外层，卡在内层求导"])
async def test_student_question_diagnosis_hints_and_independent_followup(
    setup, tmp_path, monkeypatch, initial
):
    engine, knowledge, _ = setup
    guidance = []
    domain_problem = "求 sqrt(x)*sqrt(x) 在 x 趋向 0- 时的极限"
    derivative_at_hole = "求 f(x)=(x^2-1)/(x-1) 在 x=1 处的导数"
    paraphrased_followup = "换成 t：求 sin(2t) 的三次方的导数"
    followup_spec = {"operation": "derivative", "expr": "sin(2*t)**3", "variables": ["t"]}
    limit = {"operation": "limit", "variables": ["x"], "limit_point": "0", "limit_direction": "-"}

    class ModelForQuestion(CombinedModel):
        fail_solution = initial == ""
        probe_count = 0

        async def generate(self, messages, output, tools=None):
            payload = json.loads(messages[-1].content)
            if output is QuestionDraft:
                if payload["problem"] == derivative_at_hole:
                    return QuestionDraft(
                        knowledge_point_id=POINT,
                        spec={
                            "operation": "derivative",
                            "expr": "(x**2-1)/(x-1)",
                            "variables": ["x"],
                        },
                        answer="1",
                    )
                if payload["problem"] == paraphrased_followup:
                    return QuestionDraft(
                        knowledge_point_id=POINT,
                        spec=followup_spec,
                        answer="6*sin(2*t)**2*cos(2*t)",
                    )
                if payload["problem"] == domain_problem:
                    return QuestionDraft(
                        knowledge_point_id=POINT, spec={**limit, "expr": "x"}, answer="0"
                    )
                return QuestionDraft(
                    knowledge_point_id=POINT,
                    spec={"operation": "derivative", "expr": "sin(8*x)", "variables": ["x"]},
                    answer="8*cos(8*x)" if payload["problem"] != "bad" else "7",
                )
            if output is QuestionInterpretation:
                assert set(payload) == {"problem"}
                if payload["problem"] == paraphrased_followup:
                    return QuestionInterpretation(spec=followup_spec)
                if payload["problem"] == domain_problem:
                    return QuestionInterpretation(spec={**limit, "expr": "sqrt(x)*sqrt(x)"})
                n = 4 if "4x" in payload["problem"] else 8
                return QuestionInterpretation(
                    spec={"operation": "derivative", "expr": f"sin({n}*x)", "variables": ["x"]}
                )
            if output is AttemptReview:
                assert "verified_answer" not in payload
                return AttemptReview(
                    final_answer={
                        "cos(x)": "cos(x)",
                        "还是不会": "8*cos(8*x)",  # An invented answer must not finish the task.
                        "我的答案是 $8*cos(8*x)$。": "8*cos(8*x)",
                    }.get(payload["attempt"], ""),
                    observation="需要检查内层导数",
                    stuck_step="卡在内层求导" if "卡在内层求导" in payload["attempt"] else "",
                )
            if output is Guidance:
                guidance.append(payload)
                if payload["verified_answer"]:
                    return Guidance(
                        text=" " if self.fail_solution else "外层求导得余弦，内层求导得系数八。"
                    )
                if len(guidance) % 2:
                    return Guidance(text=r"答案是 $8\cos(8x)$。")
                return Guidance(text="答案是八倍的八艾克斯的余弦。")
            if output is HintReview:
                # The equation is rejected by symbolic comparison before reaching review.
                assert "八倍" in payload["hint"]
                return HintReview(safe=False)
            if output is ProbeDraft and payload["active_ids"] and initial == "不会用链式法则":
                self.probe_count += 1
                n = 21 + self.probe_count
                return ProbeDraft(
                    stem=f"求 sin({n}x) 的导数",
                    spec={"operation": "derivative", "expr": f"sin({n}*x)", "variables": ["x"]},
                    answer=f"{n}*cos({n}*x)",
                    predictions=[
                        {
                            "misconception_id": mid,
                            "answer": f"cos({n}*x)" if mid == FORMULA else f"{n}*cos({n}*x)",
                        }
                        for mid in payload["active_ids"]
                    ],
                )
            return await super().generate(messages, output, tools)

    model, planning = ModelForQuestion(), PlanningModel()
    monkeypatch.setattr("app.main.authorize", authorize)
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(_env_file=None, auth_db_path=str(tmp_path / "auth.db")),
    )
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "question.db"),
        orchestration_dependencies={
            "tutor": create_tutor(llm=model, knowledge=knowledge, engine=engine),
            "verifier": create_verifier(llm=model, knowledge=knowledge),
            "planner": create_planner(engine=engine, llm=planning, knowledge=knowledge),
            "questions": StudentQuestions(model, knowledge),
        },
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        runtime = app.state.orchestrator
        app.state.auth_store.put_account("question-student", "fixture-only", "student", "A")
        assert (
            await client.post(
                "/api/auth/login", json={"username": "question-student", "password": "fixture-only"}
            )
        ).status_code == 200
        url = "/api/students/A/questions"
        assert (await client.post(url, json={"content": "bad"})).status_code == 422
        assert (await client.post(url, json={"content": "求 sin(4x) 的导数"})).status_code == 422
        assert (await client.post(url, json={"content": domain_problem})).status_code == 422
        assert (await client.post(url, json={"content": derivative_at_hole})).status_code == 422
        with Session(engine) as session:
            assert not session.exec(select(Task).where(Task.type == TaskType.QUESTION)).all()
        response = await client.post(url, json={"content": "求 sin(8x) 的导数", "attempt": initial})
        assert response.status_code == 202
        question = response.json()
        task_id = question["id"]
        assert question["type"] == "question"
        await runtime.wait_idle()
        if initial == "会外层，卡在内层求导":
            target = await runtime.get_answer_target(task_id)
            assert target.item_id and not target.item_id.startswith("student-question.")
            assert not target.accepts_text
            with Session(engine) as session:
                receipt = session.get(NodeReceipt, f"question:{task_id}:stuck_attempt")
                recorded = session.get(Evidence, receipt.result["evidence_id"])
                assert recorded.is_correct is None and recorded.student_answer == initial
                assert diagnosis(session, task_id).hypotheses
            return

        if not initial:

            class Vision:
                async def generate(self, messages, output, tools=None):
                    assert output is Transcription
                    return Transcription(latex="8", doubts=[])

            app.state.photo_llm = Vision()
            target = await runtime.get_answer_target(task_id)
            assert target.stem == "求 sin(8x) 的导数" and target.accepts_text
            png = BytesIO()
            Image.new("RGB", (4, 4), "white").save(png, format="PNG")
            photo = await client.post(
                f"/api/tasks/{task_id}/answer-photo",
                params={"item_id": target.item_id, "turn": target.turn},
                headers={"Content-Type": "image/png"},
                content=png.getvalue(),
            )
            assert photo.status_code == 200 and photo.json()["latex"] == "8"
            assert (await runtime.get_answer_target(task_id)).turn == target.turn

        async def reply(text, *, wait=True):
            target = await runtime.get_answer_target(task_id)
            assert target.phase == "waiting"
            result = await client.post(
                f"/api/tasks/{task_id}/messages",
                params={"expected_turn": target.turn},
                json={"content": text, "answering_item_id": target.item_id},
            )
            assert result.status_code == 202
            if wait:
                await runtime.wait_idle()

        if initial != "cos(x)":
            if not initial:
                assert "尝试" in (await runtime.get_messages(task_id))[-1].content
                await reply("不知道如何处理内层")
            assert (await runtime.get_messages(task_id))[-1].hint_level is None
            await reply("cos(x)")
        probe = (await runtime.get_messages(task_id))[-1]
        assert probe.item and not probe.item.answer and not probe.item.predictions
        if initial == "不会用链式法则":
            for _ in range(3):
                assert (await runtime.get_answer_target(task_id)).item_id
                await reply("???")
            with Session(engine) as session:
                task = session.get(Task, task_id)
                assert task.status == "cancelled" and "重规划：" in task.reason
                event = session.exec(
                    select(Event).where(Event.student_id == "A").order_by(Event.id.desc())
                ).first()
                assert event.type == "task_due" and event.processed
            return
        await reply("cos(3*x)")
        assert guidance[-1]["strategy_cards"][0]["id"] == "numeric_slope_conflict"
        for level in (1, 2, 3):
            last = (await runtime.get_messages(task_id))[-1]
            assert last.hint_level == level
            assert not last.solution
            assert all(x not in last.content for x in ("8*cos(8*x)", r"8\cos(8x)", "八倍"))
            await reply("还是不会", wait=not (level == 3 and model.fail_solution))
        if model.fail_solution:
            with pytest.raises(ExceptionGroup, match="编排后台执行失败"):
                await runtime.wait_idle()
            with Session(engine) as session:
                assert session.get(Task, task_id).status == "active"
                assert session.get(PendingWork, f"task:{task_id}").pending
            assert not (await runtime.get_messages(task_id))[-1].solution
            model.fail_solution = False
            await runtime.start_task(task_id)
            await runtime.wait_idle()
        last = (await runtime.get_messages(task_id))[-1]
        assert last.solution and "8*cos(8*x)" in last.content
        assert (await runtime.get_answer_target(task_id)).phase == "closed"
        with Session(engine) as session:
            evidence = session.get(Evidence, question["evidence_ids"][0])
            assert evidence.student_answer == initial
            assert evidence.is_correct is None
            verify = session.exec(select(Task).where(Task.type == TaskType.VERIFICATION)).one()
            assert evidence.id in verify.evidence_ids
            assert verify.knowledge_point_id == POINT
            task = session.get(Task, task_id)
            assert task.status == "done"
            followup_id = verify.id
        followup = (await runtime.get_messages(followup_id))[-1]
        assert followup.item.dimension == "correctness"
        assert followup.item.stem != "求 sin(8x) 的导数"
        response = await client.get(f"/api/tasks/{followup_id}/messages")
        assert response.status_code == 200
        shown = response.json()[-1]["item"]
        assert shown["id"] == followup.item.id and shown["answer"] == ""
        assert shown["spec"] is None and shown["fingerprint"] is None
        assert not shown["predictions"] and not shown["rubric"]
        for copied in (followup.item.stem, paraphrased_followup):
            assert (await client.post(url, json={"content": copied})).status_code == 409
        with Session(engine) as session:
            assert (
                session.exec(select(Task).where(Task.type == TaskType.QUESTION)).one().id == task_id
            )


@pytest.mark.parametrize("fault_at", ["before_checkpoint", "after_checkpoint"])
async def test_accepted_question_reply_recovers_once_without_manual_restart(
    setup, tmp_path, fault_at
):
    engine, knowledge, _ = setup

    class Questions:
        tools = SympyMathTools()
        fail_review = False

        async def solve(self, content):
            return QuestionDraft(
                knowledge_point_id=POINT,
                spec={"operation": "derivative", "expr": "sin(8*x)", "variables": ["x"]},
                answer="8*cos(8*x)",
            )

        async def review(self, item, attempt):
            if self.fail_review:
                raise RuntimeError("crash before review")
            return AttemptReview(final_answer="", observation="没有写出尝试")

        async def guide(self, *args):
            return "请补充卡住的步骤。"

    questions = Questions()
    dependencies = dict(
        planner=Planner(),
        tutor=object(),
        verifier=object(),
        knowledge=knowledge,
        questions=questions,
        pending_poll_interval=None,
    )
    checkpoint = str(tmp_path / "question-resume.db")
    async with Orchestrator.open(engine, checkpoint, EventBus(), **dependencies) as runtime:
        task = await create_question(runtime, "A", QuestionCreate(content="求 sin(8x) 的导数"))
        await runtime.wait_idle()
        target = await runtime.get_answer_target(task.id)
        original = runtime._run_task

        async def fail_before_checkpoint(key, inp):
            if inp is not None and fault_at == "before_checkpoint":
                raise RuntimeError("crash before resume")
            await original(key, inp)

        runtime._run_task = fail_before_checkpoint
        questions.fail_review = fault_at == "after_checkpoint"
        await runtime.enqueue_message(
            task.id,
            MessageCreate(content="不会做", answering_item_id=target.item_id),
            expected_turn=target.turn,
        )
        with pytest.raises(ExceptionGroup, match="编排后台执行失败"):
            await runtime.wait_idle()
        with Session(engine) as session:
            assert session.get(PendingWork, f"task:{task.id}").pending

    questions.fail_review = False
    async with Orchestrator.open(engine, checkpoint, EventBus(), **dependencies) as runtime:
        await runtime.wait_idle()
        assert (await runtime.get_answer_target(task.id)).phase == "waiting"
        messages = await runtime.get_messages(task.id)
        assert sum(message.content == "不会做" for message in messages) == 1
        assert messages[-1].content == "请补充卡住的步骤。"
        with Session(engine) as session:
            assert not session.get(PendingWork, f"task:{task.id}").pending


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["3*cos(3*x)", "-2*cos(5*x)*sin(5*x)"], MISS),
        (["cos(3*x)"], FORMULA),
        (["3*cos(3*x)", "-10*cos(5*x)*sin(5*x)"], "slip"),
    ],
)
async def test_complete_tutor_verifier_path(setup, tmp_path, answers, expected):
    engine, knowledge, task = setup
    model = CombinedModel()
    bus = EventBus()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    verifier = create_verifier(llm=model, knowledge=knowledge)
    checkpoint = str(tmp_path / "checkpoints.db")
    dependencies = dict(
        planner=Planner(),
        tutor=tutor,
        verifier=verifier,
        knowledge=knowledge,
        pending_poll_interval=None,
    )
    async with Orchestrator.open(engine, checkpoint, bus, **dependencies) as runtime:
        await runtime.start_task(task.task_id, teaching_date=TODAY + timedelta(days=1))
        for answer in answers:
            target = await runtime.get_answer_target(task.task_id)
            await runtime.submit_message(
                task.task_id, MessageCreate(content=answer, answering_item_id=target.item_id)
            )
        if expected != "slip":
            message = (await runtime.get_messages(task.task_id))[-1]
            await runtime.submit_message(
                task.task_id,
                MessageCreate(content=message.item.answer, answering_item_id=message.item.id),
            )
        # Stop at a real verification wait and recover it from disk.
    async with Orchestrator.open(engine, checkpoint, bus, **dependencies) as runtime:
        dimensions = 1 if expected == "slip" else 3
        for _ in range(dimensions):
            message = (await runtime.get_messages(task.task_id))[-1]
            assert message.item.purpose == "verification"
            await runtime.submit_message(
                task.task_id,
                MessageCreate(content=message.item.answer, answering_item_id=message.item.id),
            )
        await runtime.wait_idle()
        with Session(engine) as session:
            rows = session.exec(
                select(Evidence).where(
                    Evidence.task_id == task.task_id, Evidence.kind == EvidenceKind.VERIFICATION
                )
            ).all()
            assert len(rows) == dimensions
            diagnostic = session.exec(
                select(Evidence).where(
                    Evidence.task_id == task.task_id, Evidence.kind == EvidenceKind.DIAGNOSIS
                )
            ).all()
            assert all(e.teaching_date == TODAY + timedelta(days=1) for e in diagnostic)
            assert all(row.is_correct for row in rows)
            mastery = session.get(Mastery, ("A", POINT))
            assert mastery.state == (
                MasteryState.TUTORING if expected == "slip" else MasteryState.INITIAL
            )
            # The existing ledger does not claim multi-dimension mastery for a slip.
            assert [
                h.misconception_id
                for h in diagnosis(session, task.task_id).hypotheses
                if h.status == "supported"
            ] == [expected]
    await bus.close()


async def test_diagnosis_http_shows_unanswered_predictions(setup, tmp_path):
    engine, knowledge, task = setup
    tutor = create_tutor(llm=Model(), knowledge=knowledge, engine=engine)
    await tutor.turn(input_for(task), Publisher())
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "http.db"),
        orchestration_dependencies={"tutor": tutor, "verifier": None},
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get(f"/api/tasks/{task.task_id}/diagnosis")
        assert response.status_code == 200
        assert response.json()["probes"][0]["student_answer"] is None
        assert len(response.json()["probes"][0]["predictions"]) == 3
        assert (await client.get("/api/tasks/999/diagnosis")).status_code == 404


async def test_shared_initial_error_cannot_be_pruned_by_model(setup):
    engine, knowledge, task = setup

    class PrematureModel(Model):
        async def generate(self, messages, output, tools=None):
            if output is CandidateSelection:
                return CandidateSelection(misconception_ids=[MISS], reason="过早选择单一错因")
            return await super().generate(messages, output, tools)

    tutor = create_tutor(llm=PrematureModel(), knowledge=knowledge, engine=engine)
    await tutor.turn(input_for(task), Publisher())
    with Session(engine) as session:
        assert {h.misconception_id for h in diagnosis(session, task.task_id).hypotheses} == {
            MISS,
            FORMULA,
            "slip",
        }


@pytest.mark.parametrize(
    "disabled", [{}, {"tutor": None}, {"verifier": None}, {"tutor": None, "verifier": None}]
)
async def test_auto_tutor_shares_clock_date_and_model_client(tmp_path, monkeypatch, disabled):
    clients = []

    class Client:
        def __init__(self, settings):
            self.closed = False
            clients.append(self)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(llm_api_key="test-only", teaching_clock_initial_date=TODAY),
    )
    monkeypatch.setattr("app.main.OpenAICompatClient", Client)
    engine = make_engine(f"sqlite:///{tmp_path / 'assembly.db'}")
    try:
        app = create_app(
            engine=engine,
            checkpoint_path=str(tmp_path / "assembly-checkpoints.db"),
            orchestration_dependencies={"planner": None, **disabled},
        )
        async with app.router.lifespan_context(app):
            runtime = app.state.orchestrator
            assert len(clients) == (0 if len(disabled) == 2 else 1)
            for name in ("tutor", "verifier"):
                agent = getattr(runtime, name)
                if name in disabled:
                    assert agent is None
                else:
                    assert agent.llm is clients[0]
            await app.state.clock.advance(4)
            assert runtime.teaching_date() == TODAY + timedelta(days=4)
            if runtime.tutor is not None:
                assert runtime.tutor.teaching_date() == TODAY + timedelta(days=4)
        assert all(c.closed for c in clients)
    finally:
        engine.dispose()
