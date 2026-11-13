"""OPT-05 内部发布衔接；真实业务与 checkpoint，确定性 Agent。"""

import asyncio
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from app.contracts.agents import PlannedTask, PlannerOutput, TutorTurn
from app.contracts.api import MessageCreate
from app.contracts.entities import Event, Evidence, Item, KnowledgePoint, Mastery, Student, Task
from app.contracts.enums import EvidenceKind, MasteryState, TaskStatus
from app.contracts.events import AnswerSubmitted, HomeworkImported, read_payload
from app.db import init_db, make_engine
from app.ingest import HomeworkAnalysis, HomeworkImporter, HomeworkRow
from app.orchestration import EventBus, Orchestrator
from tests.test_orchestration import SpyVerifier

TODAY = date(2026, 9, 7)


class HomeworkModel:
    async def generate(self, messages, output, tools=None):
        assert output is HomeworkAnalysis
        return HomeworkAnalysis(
            knowledge_point_id="kp", is_correct=False, misconception_id=None, reason="作答不正确"
        )


class Planner:
    def __init__(self, engine, fail_followup=False):
        self.engine = engine
        self.fail_followup = fail_followup
        self.retest = fail_followup

    async def plan(self, inp):
        with Session(self.engine) as session:
            payload = read_payload(session.get(Event, inp.event_id))
            if isinstance(payload, AnswerSubmitted):
                task = session.get(Task, payload.task_id)
                if task.status == TaskStatus.DONE:
                    if self.fail_followup:
                        raise RuntimeError("后续复测规划暂时失败")
                    if self.retest:
                        return PlannerOutput(
                            tasks=[
                                PlannedTask(
                                    type="retest",
                                    student_id="A",
                                    knowledge_point_id="kp",
                                    evidence_ids=[payload.evidence_id],
                                    due_date=TODAY + timedelta(days=3),
                                    reason="独立验证通过后的延迟复测",
                                )
                            ]
                        )
        if not isinstance(payload, HomeworkImported):
            return PlannerOutput()
        return PlannerOutput(
            tasks=[
                PlannedTask(
                    type="tutoring",
                    student_id="A",
                    knowledge_point_id="kp",
                    evidence_ids=payload.evidence_ids,
                    reason="根据实际作业安排辅导",
                )
            ]
        )


class Tutor:
    async def turn(self, inp, publish):
        if inp.student_message is None:
            return TutorTurn(outcome="await_student", message="请说明你的思路。")
        return TutorTurn(outcome="request_verification", message="接下来独立完成新题。")


class Knowledge:
    def ancestors(self, point):
        return []


@pytest.mark.parametrize("fail_followup", [False, True])
def test_import_tutoring_verification_restart_and_reused_task(tmp_path, fail_followup):
    engine = make_engine(f"sqlite:///{tmp_path / 'workflow.db'}")
    init_db(engine)
    with Session(engine) as session:
        session.add(Student(id="A", name="同名学生"))
        session.add(Student(id="B", name="同名学生"))
        session.add(KnowledgePoint(id="kp", name="链式法则", lecture=2, section=1))
        session.commit()

    async def scenario():
        verifier = SpyVerifier()
        planner = Planner(engine, fail_followup)
        dependencies = dict(
            planner=planner,
            tutor=Tutor(),
            verifier=verifier,
            knowledge=Knowledge(),
            pending_poll_interval=None,
            teaching_date=lambda: TODAY,
        )
        checkpoint = str(tmp_path / "checkpoint.db")
        async with Orchestrator.open(engine, checkpoint, EventBus(), **dependencies) as runtime:
            result = await HomeworkImporter(engine, HomeworkModel()).import_rows(
                [
                    HomeworkRow(
                        student_id="A",
                        item_id="hw",
                        stem="求 sin(3x) 的导数",
                        correct_answer="3*cos(3*x)",
                        student_answer="cos(3*x)",
                    )
                ],
                TODAY,
                allow_new_students=False,
            )
            assert runtime.homework_workflow(result.event_id).state == "pending"
            await runtime.retry_homework(result.event_id)
            await runtime.wait_idle()
            view = runtime.homework_workflow(result.event_id)
            assert view.state == "planned" and len(view.task_ids) == 1
            task_id = view.task_ids[0]
            with Session(engine) as session:
                assert session.get(Task, task_id).student_id == "A"
                assert session.get(Mastery, ("B", "kp")) is None
                assert session.get(Mastery, ("A", "kp")).state != MasteryState.INITIAL
                assert len(session.exec(select(Evidence)).all()) == 1

            # 第二个真实事件复用仍在进行中的任务，关联保留、不会另建任务/重发开场。
            second = await HomeworkImporter(engine, HomeworkModel()).import_rows(
                [HomeworkRow(student_id="A", item_id="hw", student_answer="cos(3*x)")], TODAY
            )
            await runtime.retry_homework(second.event_id)
            await runtime.wait_idle()
            assert runtime.homework_workflow(second.event_id).task_ids == [task_id]
            assert len(await runtime.get_messages(task_id)) == 1
            await runtime.submit_message(
                task_id, MessageCreate(content="辅导尝试，仅作为辅导上下文")
            )
            assert "辅导尝试" not in verifier.inputs[0].model_dump_json()
            assert "student_id" not in verifier.inputs[0].model_dump_json()
            with Session(engine) as session:
                assert session.get(Mastery, ("A", "kp")).state != MasteryState.INITIAL

        # 从独立验证的真实等待点恢复；重复启动不重复出题。
        async with Orchestrator.open(engine, checkpoint, EventBus(), **dependencies) as runtime:
            await runtime.start_task(task_id)
            assert len(verifier.inputs) == 1
            while (target := await runtime.get_answer_target(task_id)).phase == "waiting":
                await runtime.submit_message(
                    task_id,
                    MessageCreate(content="independent-answer", answering_item_id=target.item_id),
                )
                try:
                    await runtime.wait_idle()
                except ExceptionGroup:
                    assert planner.fail_followup
                    assert runtime.homework_workflow(result.event_id).state == "failed"
                    assert "event:" in runtime.homework_workflow(result.event_id).error
                    planner.fail_followup = False
                    await runtime.retry_homework(result.event_id)
                    await runtime.wait_idle()
            with Session(engine) as session:
                assert session.get(Task, task_id).status == TaskStatus.DONE
                assert session.get(Mastery, ("A", "kp")).state == MasteryState.INITIAL
                assert session.get(Mastery, ("B", "kp")) is None
                assert len(session.exec(select(Task)).all()) == (2 if fail_followup else 1)
                assert runtime.homework_workflow(result.event_id).state == "planned"
                assert runtime.homework_workflow(result.event_id).task_ids == (
                    [task_id, task_id + 1] if fail_followup else [task_id]
                )
                evidence = session.exec(select(Evidence)).all()
                assert sum(e.kind == EvidenceKind.HOMEWORK for e in evidence) == 2
                assert sum(e.kind == EvidenceKind.VERIFICATION for e in evidence) == 3
                assert all(e.student_id == "A" for e in evidence)
                assert len(session.exec(select(Item)).all()) == 4

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
