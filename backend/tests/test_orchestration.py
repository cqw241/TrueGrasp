"""T03 验收：桩 Agent 完整交互、教考隔离、SQLite 恢复及事件分发。"""

import asyncio
from datetime import date, timedelta

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session, select

from app.contracts.agents import Judgement, PlannedTask, PlannerOutput, TutorTurn
from app.contracts.api import MessageCreate
from app.contracts.entities import (
    Event,
    Evidence,
    Item,
    KnowledgePoint,
    Mastery,
    Misconception,
    Student,
    Task,
)
from app.contracts.enums import (
    Dimension,
    EvidenceKind,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    TaskStatus,
    TaskType,
    TutorOutcome,
)
from app.contracts.events import HomeworkImported, TaskDue, TokenDelta, make_event, read_payload
from app.contracts.values import Prediction
from app.db import init_db, make_engine
from app.main import create_app
from app.orchestration import EventBus, OrchestrationError, Orchestrator, build_verifier_input
from app.orchestration.storage import PendingWork

TODAY = date(2026, 9, 7)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def business(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    with Session(engine) as session:
        session.add_all([Student(id="A", name="学生 A"), Student(id="B", name="学生 B")])
        session.add_all(
            [
                KnowledgePoint(id="kp", name="链式法则", lecture=2, section=2),
                KnowledgePoint(id="pre", name="函数复合", lecture=1, section=1),
            ]
        )
        session.flush()
        session.add(
            Misconception(
                id="miss_inner", knowledge_point_id="kp", family="chain", description="漏乘内层导数"
            )
        )
        for item_id in ["homework", "probe", "other"]:
            session.add(
                Item(
                    id=item_id,
                    knowledge_point_id="kp",
                    purpose=ItemPurpose.PROBE if item_id == "probe" else ItemPurpose.HOMEWORK,
                    stem=f"题目 {item_id}",
                    answer="correct",
                    fingerprint=f"fp-{item_id}",
                    source=ItemSource.SEED,
                )
            )
        session.flush()
        for student, item in [("A", "homework"), ("B", "other")]:
            session.add(
                Evidence(
                    student_id=student,
                    knowledge_point_id="kp",
                    kind=EvidenceKind.HOMEWORK,
                    item_id=item,
                    student_answer="SECRET_HOMEWORK" if student == "A" else "other",
                    is_correct=False,
                    judged_by=JudgedBy.TOOL,
                    teaching_date=TODAY,
                )
            )
        session.flush()
        session.add(
            make_event(HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=[1]), TODAY)
        )
        session.commit()
    yield engine
    engine.dispose()


class StubKnowledge:
    def ancestors(self, kp_id):
        return [KnowledgePoint(id="pre", name="函数复合", lecture=1, section=1)]


class StubPlanner:
    def __init__(self, engine):
        self.engine = engine
        self.calls = []
        self.cancel_task_ids = []

    async def plan(self, inp):
        self.calls.append(inp)
        with Session(self.engine) as session:
            payload = read_payload(session.get(Event, inp.event_id))
        if not isinstance(payload, HomeworkImported):
            return PlannerOutput(cancel_task_ids=self.cancel_task_ids)
        return PlannerOutput(
            tasks=[
                PlannedTask(
                    type=TaskType.TUTORING,
                    student_id="A",
                    knowledge_point_id="kp",
                    evidence_ids=[1],
                    reason="个辅",
                ),
                PlannedTask(
                    type=TaskType.RETEST,
                    student_id="A",
                    knowledge_point_id="kp",
                    reason="周五复测",
                    due_date=TODAY + timedelta(days=4),
                ),
                PlannedTask(
                    type=TaskType.CLASS_REVIEW,
                    student_id=None,
                    knowledge_point_id="kp",
                    reason="集体讲解",
                ),
                PlannedTask(
                    type=TaskType.ESCALATION,
                    student_id="B",
                    knowledge_point_id="kp",
                    reason="教师处理",
                ),
            ],
            cancel_task_ids=self.cancel_task_ids,
        )


class StubTutor:
    def __init__(self):
        self.calls = []
        self.replanning = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def turn(self, inp, publish):
        self.calls.append(inp)
        self.entered.set()
        await self.release.wait()
        assert inp.task.allowed_backtrack_ids == ["pre"]
        if self.replanning:
            return TutorTurn(
                outcome=TutorOutcome.REQUEST_REPLAN,
                message="请老师协助",
                replan_reason="三轮仍无法确诊",
            )
        if inp.student_message is None:
            await publish.publish(TokenDelta(task_id=inp.task.task_id, text="SECRET_LESSON"))
            return TutorTurn(
                outcome=TutorOutcome.AWAIT_STUDENT,
                message="SECRET_LESSON",
                awaiting_item_id="probe",
            )
        assert inp.history[-1].content == "SECRET_LESSON"
        return TutorTurn(
            outcome=TutorOutcome.REQUEST_VERIFICATION,
            message="SECRET_EXPLANATION",
            confirmed_misconception_ids=["miss_inner"],
        )


class SpyVerifier:
    def __init__(self):
        self.inputs = []
        self.judgements = []
        self.fail_judgement = False

    async def compose(self, inp):
        self.inputs.append(inp)
        return [
            Item(
                id=f"verification-{len(self.inputs)}-{dimension}",
                knowledge_point_id=inp.knowledge_point_id,
                purpose=ItemPurpose.VERIFICATION,
                dimension=dimension,
                stem=f"独立验证 {dimension}",
                answer="independent-answer",
                predictions=[Prediction(misconception_id="miss_inner", answer="wrong")],
                fingerprint=f"fp-verification-{len(self.inputs)}-{dimension}",
                source=ItemSource.GENERATED,
            )
            for dimension in inp.dimensions
        ]

    async def judge(self, item, student_answer):
        self.judgements.append((item, student_answer))
        if self.fail_judgement:
            raise RuntimeError("判定失败，保留 checkpoint")
        return Judgement(
            is_correct=student_answer == "independent-answer",
            judged_by=JudgedBy.TOOL,
            reason="独立核验",
        )


@pytest.fixture
def dependencies(business):
    return {
        "planner": StubPlanner(business),
        "tutor": StubTutor(),
        "verifier": SpyVerifier(),
        "knowledge": StubKnowledge(),
    }


async def test_pipeline_and_verifier_isolation(business, tmp_path, dependencies):
    bus = EventBus()
    async with Orchestrator.open(
        business, str(tmp_path / "checkpoint.db"), bus, **dependencies
    ) as runtime:
        async with bus.subscribe() as stream:
            await runtime.handle_event(1)
            await runtime.wait_idle()
            assert (await runtime.get_messages(1))[0].item.id == "probe"
            await runtime.submit_message(
                1, MessageCreate(content="SECRET_TUTOR_ANSWER", answering_item_id="probe")
            )
            verifier = dependencies["verifier"]
            inp = verifier.inputs[0]
            assert set(inp.model_dump()) == {
                "knowledge_point_id",
                "dimensions",
                "confirmed_misconceptions",
                "used_fingerprints",
                "context_knowledge_point_id",
            }
            assert inp.confirmed_misconceptions[0].description == "漏乘内层导数"
            assert inp.used_fingerprints == ["fp-homework", "fp-probe"]
            assert "SECRET" not in inp.model_dump_json()
            assert "student_id" not in inp.model_dump_json()
            assert "fp-other" not in inp.used_fingerprints
            for _ in range(3):
                messages = await runtime.get_messages(1)
                await runtime.submit_message(
                    1,
                    MessageCreate(
                        content="independent-answer", answering_item_id=messages[-1].item.id
                    ),
                )
                await runtime.wait_idle()
            await bus.close()
            notifications = [event async for event in stream]
        assert len(dependencies["tutor"].calls) == 2
        assert len(verifier.inputs) == 1
        assert len(verifier.judgements) == 3
        assert all(answer == "independent-answer" for _, answer in verifier.judgements)
        assert sum(event.type == "evidence_added" for event in notifications) == 3
        assert sum(event.type == "message_done" for event in notifications) == 5
        with Session(business) as session:
            assert session.get(Event, 1).processed
            assert session.get(Task, 1).status == TaskStatus.DONE
            assert session.get(Task, 2).status == TaskStatus.PENDING
            assert session.get(Task, 3).status == TaskStatus.PENDING
            assert session.get(Task, 4).status == TaskStatus.PENDING
            evidence = session.exec(
                select(Evidence).where(Evidence.kind == EvidenceKind.VERIFICATION)
            ).all()
            assert len(evidence) == 3
            assert all(
                e.teaching_date == TODAY
                and e.correct_answer == "independent-answer"
                and e.reason == "独立核验"
                for e in evidence
            )
            assert all(e.predictions[0].answer == "wrong" for e in evidence)
            assert session.exec(select(Mastery)).all() == []
            assert all(event.processed for event in session.exec(select(Event)).all())


async def test_checkpoint_restart_and_duplicate_start(business, tmp_path, dependencies):
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
        await runtime.start_task(1)
        assert len(dependencies["tutor"].calls) == 1
        assert len(await runtime.get_messages(1)) == 1
        await runtime.submit_message(
            1, MessageCreate(content="SECRET_TUTOR_ANSWER", answering_item_id="probe")
        )
        first_question = (await runtime.get_messages(1))[-1].item.id
        await runtime.start_task(1)
        assert len(dependencies["verifier"].inputs) == 1
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        assert (await runtime.get_messages(1))[-1].item.id == first_question
        await runtime.submit_message(
            1, MessageCreate(content="independent-answer", answering_item_id=first_question)
        )
        await runtime.wait_idle()
        second_question = (await runtime.get_messages(1))[-1].item.id
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.start_task(1)
        assert (await runtime.get_messages(1))[-1].item.id == second_question
        assert len(dependencies["verifier"].inputs) == 1
        with Session(business) as session:
            assert len(session.exec(select(Evidence).where(Evidence.task_id == 1)).all()) == 1


async def test_replanning_and_cancellation(business, tmp_path, dependencies):
    dependencies["tutor"].replanning = True
    async with Orchestrator.open(
        business, str(tmp_path / "checkpoint.db"), EventBus(), **dependencies
    ) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
        assert len(dependencies["planner"].calls) == 2
        assert len(dependencies["tutor"].calls) == 1
        with Session(business) as session:
            assert session.get(Task, 1).status == TaskStatus.CANCELLED
            assert "三轮仍无法确诊" in session.get(Task, 1).reason
            assert isinstance(read_payload(session.get(Event, 2)), TaskDue)
        dependencies["planner"].cancel_task_ids = [2]
        with Session(business) as session:
            event = make_event(TaskDue(task_id=2), TODAY + timedelta(days=4), "A")
            session.add(event)
            session.commit()
            event_id = event.id
        await runtime.handle_event(event_id)
        await runtime.wait_idle()
        with Session(business) as session:
            assert session.get(Task, 2).status == TaskStatus.CANCELLED
        with pytest.raises(OrchestrationError, match="任务已结束"):
            await runtime.submit_message(1, MessageCreate(content="不应受理"))


async def test_due_task_and_explicit_verification_context(business, tmp_path, dependencies):
    async with Orchestrator.open(
        business, str(tmp_path / "checkpoint.db"), EventBus(), **dependencies
    ) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
        with Session(business) as session:
            event = make_event(TaskDue(task_id=2), TODAY + timedelta(days=4), "A")
            session.add(event)
            session.commit()
            event_id = event.id
        await runtime.handle_event(event_id)
        await runtime.wait_idle()
        assert dependencies["verifier"].inputs[0].dimensions == [Dimension.RETENTION]
        with Session(business) as session:
            task = Task(
                type=TaskType.PRECHECK, student_id="A", knowledge_point_id="kp", created_on=TODAY
            )
            session.add(task)
            session.commit()
            task_id = task.id
        await runtime.start_task(
            task_id, dimensions=[Dimension.CROSS_CHAPTER], context_knowledge_point_id="pre"
        )
        inp = dependencies["verifier"].inputs[-1]
        assert inp.dimensions == [Dimension.CROSS_CHAPTER]
        assert inp.context_knowledge_point_id == "pre"


async def test_slip_and_missing_misconception(business, tmp_path, dependencies):
    with Session(business) as session:
        task = Task(
            type=TaskType.VERIFICATION,
            student_id="A",
            knowledge_point_id="kp",
            misconception_ids=["slip"],
            created_on=TODAY,
        )
        session.add(task)
        session.commit()
        task_id = task.id
        with pytest.raises(ValueError, match="错因不存在"):
            build_verifier_input(
                session,
                student_id="A",
                knowledge_point_id="kp",
                dimensions=[],
                confirmed_misconception_ids=["unknown"],
                shown_item_ids=[],
            )
    async with Orchestrator.open(
        business, str(tmp_path / "checkpoint.db"), EventBus(), **dependencies
    ) as runtime:
        await runtime.start_task(task_id)
        inp = dependencies["verifier"].inputs[0]
        assert inp.dimensions == [Dimension.CORRECTNESS]
        assert inp.confirmed_misconceptions == []


async def test_judgement_failure_retries_without_duplicate_question(
    business, tmp_path, dependencies
):
    async with Orchestrator.open(
        business, str(tmp_path / "checkpoint.db"), EventBus(), **dependencies
    ) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
        await runtime.submit_message(
            1, MessageCreate(content="SECRET_TUTOR_ANSWER", answering_item_id="probe")
        )
        question = (await runtime.get_messages(1))[-1].item.id
        dependencies["verifier"].fail_judgement = True
        with pytest.raises(RuntimeError, match="判定失败"):
            await runtime.submit_message(
                1, MessageCreate(content="independent-answer", answering_item_id=question)
            )
        with Session(business) as session:
            assert session.get(Task, 1).status == TaskStatus.ACTIVE
            assert session.exec(select(Evidence).where(Evidence.task_id == 1)).all() == []
        dependencies["verifier"].fail_judgement = False
        await runtime.start_task(1)
        await runtime.wait_idle()
        messages = await runtime.get_messages(1)
        assert (
            sum(
                message.role == "user" and message.content == "independent-answer"
                for message in messages
            )
            == 1
        )
        with Session(business) as session:
            assert len(session.exec(select(Evidence).where(Evidence.task_id == 1)).all()) == 1


async def test_http_acceptance_and_validation(business, tmp_path, dependencies):
    app = create_app(
        engine=business,
        checkpoint_path=str(tmp_path / "checkpoint.db"),
        orchestration_dependencies=dependencies,
    )
    async with app.router.lifespan_context(app):
        runtime = app.state.orchestrator
        await runtime.handle_event(1)
        await runtime.wait_idle()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/tasks/1/start")).status_code == 202
            assert (await client.get("/api/tasks/1/messages")).json()[0]["item"]["id"] == "probe"
            for path in ["/api/tasks/999/start", "/api/tasks/999/messages"]:
                assert (await client.post(path, json={"content": "x"})).status_code == 404
            assert (await client.get("/api/tasks/999/messages")).status_code == 404
            assert (await client.post("/api/tasks/2/start")).status_code == 409
            assert (await client.post("/api/tasks/3/start")).status_code == 409
            assert (
                await client.post(
                    "/api/tasks/1/messages",
                    json={"content": "x", "answering_item_id": "wrong-item"},
                )
            ).status_code == 409
            result = await client.post(
                "/api/tasks/1/messages",
                json={"content": "SECRET_TUTOR_ANSWER", "answering_item_id": "probe"},
            )
            assert result.status_code == 202
            assert result.json() == {"task_id": 1}
            await runtime.wait_idle()
            messages = (await client.get("/api/tasks/1/messages")).json()
            assert messages[-1]["item"]["dimension"] == "correctness"


async def test_http_returns_before_agent_finishes_and_rejects_overlapping_reply(
    business, tmp_path, dependencies
):
    app = create_app(
        engine=business,
        checkpoint_path=str(tmp_path / "checkpoint.db"),
        orchestration_dependencies=dependencies,
    )
    tutor = dependencies["tutor"]
    tutor.release.clear()
    async with app.router.lifespan_context(app):
        with Session(business) as session:
            session.add(
                Task(
                    type=TaskType.TUTORING,
                    student_id="A",
                    knowledge_point_id="kp",
                    created_on=TODAY,
                )
            )
            session.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async with asyncio.timeout(3):
                response = await client.post("/api/tasks/1/start")
                assert response.status_code == 202
                await tutor.entered.wait()
                assert (await client.post("/api/tasks/1/start")).status_code == 202
                assert (await client.get("/api/tasks/1/messages")).json() == []
                assert (
                    await client.post("/api/tasks/1/messages", json={"content": "x"})
                ).status_code == 409
            tutor.release.set()
            async with app.state.event_bus.subscribe() as stream:
                await app.state.orchestrator.wait_idle()
                while (await anext(stream)).type != "message_done":
                    pass
                messages = (await client.get("/api/tasks/1/messages")).json()
                assert messages[-1]["content"] == "SECRET_LESSON"


async def test_unconfigured_agents_and_sse_http(business, tmp_path):
    app = create_app(engine=business, checkpoint_path=str(tmp_path / "checkpoint.db"))
    with Session(business) as session:
        session.add(
            Task(type=TaskType.TUTORING, student_id="A", knowledge_point_id="kp", created_on=TODAY)
        )
        session.commit()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/health")).json() == {"status": "ok"}
        assert (await client.get("/api/tasks/1/messages")).json() == []
        assert (await client.post("/api/tasks/1/start")).status_code == 501
        bus = app.state.event_bus
        request = asyncio.create_task(client.get("/api/events"))
        async with asyncio.timeout(3):
            while not bus._subscribers:
                await asyncio.sleep(0)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not bus._subscribers
            request = asyncio.create_task(client.get("/api/events"))
            while not bus._subscribers:
                await asyncio.sleep(0)
            await bus.publish(TokenDelta(task_id=1, text="流式内容"))
            await bus.close()
            response = await request
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"type":"token"' in response.text
        assert '"text":"流式内容"' in response.text
        assert not bus._subscribers


async def test_bus_broadcast_disconnect_and_close():
    bus = EventBus()
    event = TokenDelta(task_id=1, text="token")
    async with bus.subscribe() as first:
        async with bus.subscribe() as second:
            await bus.publish(event)
            assert await anext(first) == await anext(second) == event
        assert len(bus._subscribers) == 1
        await bus.publish(TokenDelta(task_id=2, text="next"))
        assert (await anext(first)).task_id == 2
        await bus.close()
        assert [item async for item in first] == []
    async with bus.subscribe() as closed:
        assert [item async for item in closed] == []
    assert not bus._subscribers


async def test_background_failure_can_resume(business, tmp_path, dependencies, caplog):
    tutor = dependencies["tutor"]
    original_turn = tutor.turn

    async def fail(*args):
        raise RuntimeError("模型暂时失败")

    tutor.turn = fail
    async with Orchestrator.open(
        business, str(tmp_path / "checkpoint.db"), EventBus(), **dependencies
    ) as runtime:
        await runtime.handle_event(1)
        with pytest.raises(ExceptionGroup, match="编排后台执行失败"):
            await runtime.wait_idle()
        assert "task:1" in caplog.text
        with Session(business) as session:
            assert session.get(Task, 1).status == TaskStatus.ACTIVE
        tutor.turn = original_turn
        await runtime.start_task(1)
        assert (await runtime.get_messages(1))[-1].item.id == "probe"


async def test_planner_cancels_a_waiting_session(business, tmp_path, dependencies):
    async with Orchestrator.open(
        business, str(tmp_path / "checkpoint.db"), EventBus(), **dependencies
    ) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
        dependencies["planner"].cancel_task_ids = [1]
        with Session(business) as session:
            event = make_event(TaskDue(task_id=1), TODAY, "A")
            session.add(event)
            session.commit()
            event_id = event.id
        await runtime.handle_event(event_id)
        await runtime.wait_idle()
        with pytest.raises(OrchestrationError, match="任务已结束"):
            await runtime.submit_message(1, MessageCreate(content="x", answering_item_id="probe"))
        assert len(dependencies["tutor"].calls) == 1
        assert (await runtime.get_messages(1))[-1].item.id == "probe"


async def test_students_have_independent_waiting_points(business, tmp_path, dependencies):
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
        with Session(business) as session:
            task = Task(
                type=TaskType.TUTORING,
                student_id="B",
                knowledge_point_id="kp",
                created_on=TODAY,
            )
            session.add(task)
            session.commit()
            other_task_id = task.id
        await runtime.start_task(other_task_id)
        await runtime.submit_message(
            1, MessageCreate(content="SECRET_TUTOR_ANSWER", answering_item_id="probe")
        )
        assert (await runtime.get_messages(1))[-1].item.dimension == Dimension.CORRECTNESS
        assert len(await runtime.get_messages(other_task_id)) == 1
        assert [inp.task.task_id for inp in dependencies["tutor"].calls] == [1, other_task_id, 1]


async def test_replanning_does_not_start_another_tutoring_loop(business, tmp_path, dependencies):
    dependencies["tutor"].replanning = True
    planner = dependencies["planner"]
    initial_plan = planner.plan

    async def plan(inp):
        if inp.event_id == 1:
            return await initial_plan(inp)
        planner.calls.append(inp)
        return PlannerOutput(
            tasks=[
                PlannedTask(
                    type=TaskType.TUTORING,
                    student_id="A",
                    knowledge_point_id="kp",
                    round=2,
                    reason="重新安排",
                )
            ]
        )

    planner.plan = plan
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.handle_event(1)
        async with asyncio.timeout(3):
            await runtime.wait_idle()
        assert len(dependencies["tutor"].calls) == 1
        assert len(planner.calls) == 2
        with Session(business) as session:
            assert session.get(Task, 5).status == TaskStatus.PENDING


async def cancel_after_task_commit(runtime, action, target_status):
    """在业务 commit 之后、节点返回 checkpoint 之前模拟取消进程执行。"""
    committed = asyncio.Event()
    publish = runtime._publish_task

    async def pause(task):
        if task.status == target_status:
            committed.set()
            await asyncio.Event().wait()
        await publish(task)

    runtime._publish_task = pause
    execution = asyncio.create_task(action())
    async with asyncio.timeout(3):
        await committed.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution


async def test_recover_plan_after_business_commit_before_checkpoint(
    business, tmp_path, dependencies
):
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await cancel_after_task_commit(runtime, lambda: runtime.handle_event(1), TaskStatus.PENDING)
        with Session(business) as session:
            assert session.get(Event, 1).processed
            assert len(session.exec(select(Task)).all()) == 4
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.handle_event(1)
        await runtime.wait_idle()
        assert len(dependencies["tutor"].calls) == 1
        assert len(dependencies["planner"].calls) == 1
        with Session(business) as session:
            assert len(session.exec(select(Task)).all()) == 4
            assert session.get(Task, 1).status == TaskStatus.ACTIVE


@pytest.mark.parametrize("outcome", ["finish", "replan"])
async def test_recover_terminal_commit_before_checkpoint(business, tmp_path, dependencies, outcome):
    with Session(business) as session:
        task = Task(
            type=TaskType.TUTORING if outcome == "replan" else TaskType.VERIFICATION,
            student_id="A",
            knowledge_point_id="kp",
            misconception_ids=["slip"],
            created_on=TODAY,
        )
        session.add(task)
        session.commit()
        task_id = task.id
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        if outcome == "replan":
            dependencies["tutor"].replanning = True

            async def action():
                await runtime.start_task(task_id)

            terminal_status = TaskStatus.CANCELLED
        else:
            await runtime.start_task(task_id)
            item_id = (await runtime.get_messages(task_id))[-1].item.id

            async def action():
                await runtime.submit_message(
                    task_id, MessageCreate(content="independent-answer", answering_item_id=item_id)
                )

            terminal_status = TaskStatus.DONE
        await cancel_after_task_commit(runtime, action, terminal_status)
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.start_task(task_id)
        await runtime.wait_idle()
        with Session(business) as session:
            assert session.get(Task, task_id).status == terminal_status
            events = session.exec(select(Event).where(Event.id != 1)).all()
            assert len(events) == 1
            assert events[0].processed
            if outcome == "replan":
                assert session.get(Task, task_id).reason.count("重规划：") == 1
            else:
                assert (
                    len(session.exec(select(Evidence).where(Evidence.task_id == task_id)).all())
                    == 1
                )
        snapshot = await runtime.graph.aget_state(runtime._config(f"task:{task_id}"))
        assert not snapshot.next


async def test_verifier_excludes_unanswered_items_from_other_sessions(
    business, tmp_path, dependencies
):
    class RepeatingVerifier(SpyVerifier):
        async def compose(self, inp):
            items = await super().compose(inp)
            items[0].fingerprint = (
                "new-fingerprint"
                if "shared-fingerprint" in inp.used_fingerprints
                else "shared-fingerprint"
            )
            return items

    verifier = RepeatingVerifier()
    dependencies["verifier"] = verifier
    with Session(business) as session:
        for student_id in ["A", "A", "B"]:
            session.add(
                Task(
                    type=TaskType.VERIFICATION,
                    student_id=student_id,
                    knowledge_point_id="kp",
                    misconception_ids=["slip"],
                    created_on=TODAY,
                )
            )
        session.commit()
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.start_task(1)
        first_item = (await runtime.get_messages(1))[-1].item
        assert first_item.fingerprint == "shared-fingerprint"
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.start_task(2)
        second_item = (await runtime.get_messages(2))[-1].item
        assert "shared-fingerprint" in verifier.inputs[-1].used_fingerprints
        assert first_item.id != second_item.id
        assert first_item.fingerprint != second_item.fingerprint
        await runtime.start_task(3)
        assert "shared-fingerprint" not in verifier.inputs[-1].used_fingerprints
        assert (await runtime.get_messages(3))[-1].item.fingerprint == "shared-fingerprint"


@pytest.mark.parametrize("stage", ["plan", "finish", "replan"])
async def test_startup_recovers_after_checkpoint_before_dispatch(
    business, tmp_path, dependencies, stage
):
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        if stage == "plan":
            key = "event:1"

            async def action():
                await runtime.handle_event(1)

        else:
            with Session(business) as session:
                session.add(
                    Task(
                        type=TaskType.TUTORING if stage == "replan" else TaskType.VERIFICATION,
                        student_id="A",
                        knowledge_point_id="kp",
                        misconception_ids=["slip"],
                        created_on=TODAY,
                    )
                )
                session.commit()
            key = "task:1"
            if stage == "replan":
                dependencies["tutor"].replanning = True

                async def action():
                    await runtime.start_task(1)

            else:
                await runtime.start_task(1)
                item_id = (await runtime.get_messages(1))[-1].item.id

                async def action():
                    await runtime.submit_message(
                        1, MessageCreate(content="independent-answer", answering_item_id=item_id)
                    )

        acknowledge = runtime._acknowledge

        def stop_before_dispatch(work_key):
            if work_key == key:
                raise RuntimeError("模拟 checkpoint 保存后中断")
            acknowledge(work_key)

        runtime._acknowledge = stop_before_dispatch
        with pytest.raises(RuntimeError, match="checkpoint 保存后中断"):
            await action()
        snapshot = await runtime.graph.aget_state(runtime._config(key))
        assert not snapshot.next
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        # 不再次发起原请求，仅依靠持久待办自动恢复。
        await runtime.wait_idle()
        with Session(business) as session:
            assert session.exec(select(PendingWork).where(PendingWork.pending)).all() == []
            if stage == "plan":
                assert len(session.exec(select(Task)).all()) == 4
                assert session.get(Task, 1).status == TaskStatus.ACTIVE
                assert len(dependencies["tutor"].calls) == 1
            else:
                status = TaskStatus.CANCELLED if stage == "replan" else TaskStatus.DONE
                assert session.get(Task, 1).status == status
                events = session.exec(select(Event).where(Event.id != 1)).all()
                assert len(events) == 1
                assert events[0].processed
        if stage != "plan":
            with pytest.raises(OrchestrationError, match="任务已结束"):
                await runtime.start_task(1)


async def test_startup_recovers_evidence_commit_without_rejudging(business, tmp_path, dependencies):
    with Session(business) as session:
        session.add(
            Task(
                type=TaskType.VERIFICATION,
                student_id="A",
                knowledge_point_id="kp",
                misconception_ids=["slip"],
                created_on=TODAY,
            )
        )
        session.commit()
    path = str(tmp_path / "checkpoint.db")
    bus = EventBus()
    async with Orchestrator.open(business, path, bus, **dependencies) as runtime:
        await runtime.start_task(1)
        item_id = (await runtime.get_messages(1))[-1].item.id
        committed = asyncio.Event()
        publish = bus.publish

        async def pause(event):
            if event.type == "evidence_added":
                committed.set()
                await asyncio.Event().wait()
            await publish(event)

        bus.publish = pause
        execution = asyncio.create_task(
            runtime.submit_message(
                1, MessageCreate(content="independent-answer", answering_item_id=item_id)
            )
        )
        async with asyncio.timeout(3):
            await committed.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    async with Orchestrator.open(business, path, EventBus(), **dependencies) as runtime:
        await runtime.wait_idle()
        with Session(business) as session:
            assert session.get(Task, 1).status == TaskStatus.DONE
            assert len(session.exec(select(Evidence).where(Evidence.task_id == 1)).all()) == 1
            assert len(session.exec(select(Event).where(Event.id != 1)).all()) == 1
            assert session.get(Event, 2).processed
            assert session.exec(select(PendingWork).where(PendingWork.pending)).all() == []
        assert len(dependencies["verifier"].judgements) == 1
