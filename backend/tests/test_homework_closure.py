"""OPT-05: authorized review/publish through student work and teacher evidence."""

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.contracts.agents import PlannedTask, PlannerOutput
from app.contracts.entities import Event, Evidence, Task
from app.contracts.enums import EvidenceKind, TaskStatus
from app.contracts.events import AnswerSubmitted, HomeworkImported, read_payload
from tests.test_homework_drafts import login
from tests.test_homework_drafts import system as draft_fixture
from tests.test_homework_review import Analysis, confirm, setup_review
from tests.test_homework_workflow import Knowledge, Tutor
from tests.test_orchestration import SpyVerifier

closure_system = draft_fixture


class ClassPlanner:
    def __init__(self, engine, failure):
        self.engine = engine
        self.failure = failure

    async def plan(self, inp):
        with Session(self.engine) as session:
            event = session.get(Event, inp.event_id)
            payload = read_payload(event)
            if isinstance(payload, HomeworkImported):
                if self.failure == "planner":
                    raise RuntimeError("temporary planning failure")
                evidence = session.get(Evidence, payload.evidence_ids[0])
                return PlannerOutput(
                    tasks=[
                        PlannedTask(
                            type="tutoring",
                            student_id=evidence.student_id,
                            knowledge_point_id="kp",
                            evidence_ids=payload.evidence_ids,
                            reason="依据本班确认的作业安排辅导",
                        )
                    ]
                )
            if isinstance(payload, AnswerSubmitted):
                task = session.get(Task, payload.task_id)
                if task.status == TaskStatus.DONE:
                    if self.failure == "followup":
                        raise RuntimeError("temporary followup failure")
                    answers = session.exec(
                        select(Evidence).where(
                            Evidence.task_id == task.id,
                            Evidence.kind == EvidenceKind.VERIFICATION,
                        )
                    ).all()
                    passed = all(answer.is_correct is True for answer in answers)
                    return PlannerOutput(
                        tasks=[
                            PlannedTask(
                                type="retest" if passed else "tutoring",
                                student_id=task.student_id,
                                knowledge_point_id="kp",
                                evidence_ids=[answer.id for answer in answers],
                                due_date=event.teaching_date + timedelta(days=3)
                                if passed
                                else None,
                                reason="延迟复测" if passed else "独立验证未通过，继续辅导",
                                round=task.round + 1,
                            )
                        ]
                    )
        return PlannerOutput()


class FailingTutor(Tutor):
    fail = False

    async def turn(self, inp, publish):
        if self.fail:
            raise RuntimeError("temporary tutoring failure")
        return await super().turn(inp, publish)


@pytest.mark.parametrize("failure", ["planner", "tutor", "judge", "followup", "incorrect"])
def test_publication_student_teacher_closure_and_authorized_recovery(closure_system, failure):
    client, app, engine = closure_system
    runtime = app.state.orchestrator
    planner, tutor, verifier = ClassPlanner(engine, failure), FailingTutor(), SpyVerifier()
    runtime.planner, runtime.tutor = planner, tutor
    runtime.verifier, runtime.knowledge = verifier, Knowledge()
    tutor.fail = failure == "tutor"
    path, draft, group, member = setup_review(client)
    assert (
        client.post(
            "/api/goals",
            params={"class_id": group["class_id"]},
            json={"knowledge_point_ids": ["kp"], "deadline": "2026-09-14", "max_rounds": 3},
        ).status_code
        == 200
    )
    client.portal.call(runtime.wait_idle)
    # The same visible name must never redirect a task to another stable identity.
    other = client.post(
        f"/api/classes/{group['class_id']}/students",
        json={
            "name": member["name"],
            "student_number": "202602",
        },
    ).json()
    draft = confirm(client, path, draft, member)
    app.state.homework_llm = Analysis()
    body = {
        "expected_revision": draft["revision"],
        "record_ids": [draft["records"][0]["record_id"]],
    }
    response = client.post(path + "/publish", json=body, headers={"Idempotency-Key": "closure"})
    assert response.status_code == 201, response.text
    publication = response.json()
    base = f"/api/homework-publications/{publication['publication_id']}"

    def idle(failed=False):
        if failed:
            with pytest.raises(ExceptionGroup):
                client.portal.call(runtime.wait_idle)
        else:
            client.portal.call(runtime.wait_idle)

    def recover():
        view = client.get(base + "/progress").json()
        assert view["workflow"]["state"] == "failed"
        assert view["workflow"]["error"]["retryable"]
        login(client, "teacher2")
        assert client.post(base + "/workflow-retries").status_code == 404
        assert client.get(base + "/progress").status_code == 404
        login(client)
        planner.failure = None
        tutor.fail = verifier.fail_judgement = False
        assert client.post(base + "/workflow-retries").status_code == 202
        assert client.post(base + "/workflow-retries").status_code == 202
        idle()
        assert client.get(base).json()["workflow"]["state"] == "planned"

    idle(failure in ("planner", "tutor"))
    if failure in ("planner", "tutor"):
        recover()
    progress = client.get(base + "/progress").json()
    assert len(progress["tasks"]) == 1
    entry = progress["tasks"][0]
    task_id = entry["task"]["id"]
    assert entry["task"]["student_id"] == member["student_id"]
    assert entry["student_number"] == "202601"
    assert entry["mastery_state"] != "initial"
    assert all(v["evidence_id"] is None for v in entry["verification"])
    setup = client.post(
        f"/api/classes/{group['class_id']}/students/{member['student_id']}/setup-link"
    ).json()
    password = "test-student-password"
    assert (
        client.post(
            "/api/auth/student-setup",
            json={
                "token": setup["setup_path"].split("#")[1],
                "password": password,
            },
        ).status_code
        == 200
    )
    username = setup["username"]

    def student_login():
        assert (
            client.post(
                "/api/auth/login", json={"username": username, "password": password}
            ).status_code
            == 200
        )

    student_login()
    assert client.get(base + "/progress").status_code == 403
    assert client.post(base + "/workflow-retries").status_code == 403
    tasks = client.get("/api/tasks").json()
    assert len(tasks) == 1 and tasks[0]["id"] == task_id
    assert client.get(f"/api/students/{other['student_id']}/mastery").status_code == 403
    for turn in range(4):
        target = client.get(f"/api/tasks/{task_id}/answer-target").json()
        assert target["phase"] == "waiting"
        assert target["accepts_text"] == (turn == 0)
        visible = client.get(f"/api/tasks/{task_id}/messages").json()
        assert not any(m.get("item") and m["item"]["answer"] for m in visible)
        answer = "我理解了内层导数，给出独立计算" if turn == 0 else "independent-answer"
        if failure == "incorrect" and turn == 1:
            answer = "wrong"
        verifier.fail_judgement = failure == "judge" and turn == 1
        sent = client.post(
            f"/api/tasks/{task_id}/messages",
            json={
                "content": answer,
                "answering_item_id": target["item_id"],
            },
        )
        assert sent.status_code == 202, sent.text
        failed = (failure == "judge" and turn == 1) or (failure == "followup" and turn == 3)
        idle(failed)
        if failed:
            login(client)
            recover()
            student_login()
    assert client.get(f"/api/tasks/{task_id}/answer-target").json()["phase"] == "closed"
    login(client)
    final = client.get(base + "/progress").json()
    assert final["workflow"]["state"] == "planned"
    assert len(final["tasks"]) == 2
    done, retest = final["tasks"]
    assert done["task"]["status"] == "done"
    assert (done["mastery_state"] == "initial") == (failure != "incorrect")
    assert sum(v["passed"] is True for v in done["verification"]) == (
        2 if failure == "incorrect" else 3
    )
    for result in done["verification"]:
        if result["evidence_id"]:
            assert client.get(f"/api/evidence/{result['evidence_id']}").status_code == 200
    if failure == "incorrect":
        assert retest["task"]["type"] == "tutoring" and retest["task"]["status"] == "active"
    else:
        assert retest["task"]["type"] == "retest" and retest["task"]["status"] == "pending"
    assert client.get("/api/class/overview", params={"class_id": group["class_id"]}).json()[
        "goal_met"
    ] == (0 if failure == "incorrect" else 1)
    replay = client.post(path + "/publish", json=body, headers={"Idempotency-Key": "closure"})
    assert replay.status_code == 200
    assert replay.json()["publication_id"] == publication["publication_id"]
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).all()
        assert sum(e.kind == EvidenceKind.HOMEWORK for e in evidence) == 1
        assert sum(e.kind == EvidenceKind.VERIFICATION for e in evidence) == 3
        assert all(e.student_id == member["student_id"] for e in evidence)
        assert len(session.exec(select(Task)).all()) == 2
