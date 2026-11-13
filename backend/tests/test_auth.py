"""认证、对象授权和事件隔离验收。"""

import asyncio
import importlib
import io
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session

from app.api.stream import events, visible
from app.auth import COOKIE, AuthStore, Principal
from app.classroom import Classroom, Enrollment, StudentPlacement
from app.config import get_settings
from app.contracts.entities import Evidence, KnowledgePoint, Mastery, Student, Task
from app.contracts.enums import EvidenceKind, JudgedBy, TaskType
from app.contracts.events import EvidenceAdded, MessageDone, TokenDelta
from app.db import init_db, make_engine
from app.main import create_app
from app.orchestration.bus import EventBus
from tests.test_tutor import TODAY, Model
from tests.test_tutor import setup as setup


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "auth.db"))
    get_settings.cache_clear()
    auth_store = AuthStore(str(tmp_path / "auth.db"))
    auth_store.put_account("teacher", "teacher-pass", "teacher", None)
    with auth_store.connect() as auth_db:
        teacher_id = auth_db.execute(
            "SELECT teacher_id FROM account WHERE username='teacher'"
        ).fetchone()[0]
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    today = date(2026, 9, 7)
    with Session(engine) as db:
        db.add_all(
            [
                Classroom(id="cls_a", teacher_id=teacher_id, name="班级 A"),
                Classroom(id="cls_b", teacher_id=teacher_id, name="班级 B"),
                Student(id="A", name="A"),
                Student(id="B", name="B"),
            ]
        )
        db.flush()
        db.add_all(
            [
                Enrollment(class_id="cls_a", student_id="A"),
                Enrollment(class_id="cls_b", student_id="B"),
                StudentPlacement(student_id="A", teacher_id=teacher_id),
                StudentPlacement(student_id="B", teacher_id=teacher_id),
            ]
        )
        db.add(KnowledgePoint(id="kp", name="KP", lecture=1, section=1))
        db.flush()
        db.add_all(
            [
                Task(
                    id=1,
                    type=TaskType.TUTORING,
                    student_id="A",
                    knowledge_point_id="kp",
                    class_id="cls_a",
                    created_on=today,
                ),
                Task(
                    id=2,
                    type=TaskType.TUTORING,
                    student_id="B",
                    knowledge_point_id="kp",
                    class_id="cls_b",
                    created_on=today,
                ),
            ]
        )
        db.add_all(
            [
                Evidence(
                    id=1,
                    student_id="A",
                    knowledge_point_id="kp",
                    class_id="cls_a",
                    kind=EvidenceKind.HOMEWORK,
                    judged_by=JudgedBy.TOOL,
                    teaching_date=today,
                ),
                Evidence(
                    id=2,
                    student_id="B",
                    knowledge_point_id="kp",
                    class_id="cls_b",
                    kind=EvidenceKind.HOMEWORK,
                    judged_by=JudgedBy.TOOL,
                    teaching_date=today,
                ),
            ]
        )
        db.commit()
    app = create_app(engine=engine, checkpoint_path=str(tmp_path / "cp.db"))
    with TestClient(app) as client:
        store = app.state.auth_store
        store.put_account("alice", "alice-pass", "student", "A")
        store.put_account("bob", "bob-pass", "student", "B")
        yield client, app, engine
    engine.dispose()
    get_settings.cache_clear()


def login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_roles_objects_and_csrf(app_client):
    client, app, _ = app_client
    assert client.get("/api/students").status_code == 401
    assert login(client, "alice", "bad").status_code == 401
    result = login(client, "alice", "alice-pass")
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"]
    assert result.json()["student_id"] == "A"
    students = client.get("/api/students")
    assert students.status_code == 200, students.text
    assert [s["id"] for s in students.json()] == ["A"]
    assert [t["id"] for t in client.get("/api/tasks").json()] == [1]
    assert client.get("/api/tasks?student_id=B").status_code == 403
    assert client.get("/api/students/B/timeline").status_code == 403
    assert client.get("/api/students/B/verification?knowledge_point_id=kp").status_code == 403
    assert client.post("/api/students/B/question-photo").status_code == 403
    assert client.post("/api/tasks/2/start").status_code == 404
    assert client.get("/api/tasks/2/messages").status_code == 404
    assert client.post("/api/tasks/2/answer-photo?item_id=x&turn=0").status_code == 404
    assert client.post("/api/tasks/2/question-photo").status_code == 404
    assert client.get("/api/tasks/2/diagnosis").status_code == 404
    assert client.get("/api/evidence/1").status_code == 200
    assert client.get("/api/evidence/2").status_code == 404
    assert client.get("/api/class/overview").status_code == 403
    assert (
        client.post("/api/auth/logout", headers={"origin": "https://evil.test"}).status_code == 403
    )
    token = client.cookies.get(COOKIE)
    assert client.post("/api/auth/logout").status_code == 204
    assert app.state.auth_store.principal(token) is None
    assert client.get("/api/auth/me").status_code == 401
    assert login(client, "teacher", "teacher-pass").status_code == 200
    assert len(client.get("/api/students").json()) == 2
    assert len(client.get("/api/tasks").json()) == 2
    assert client.get("/api/evidence/2").status_code == 200


def test_expiry_and_account_update(app_client):
    client, app, _ = app_client
    login(client, "alice", "alice-pass")
    token = client.cookies.get(COOKIE)
    with app.state.auth_store.connect() as db:
        db.execute("UPDATE auth_session SET expires=0")
    assert client.get("/api/auth/me").status_code == 401
    assert app.state.auth_store.principal(token) is None
    login(client, "alice", "alice-pass")
    token = client.cookies.get(COOKIE)
    app.state.auth_store.put_account("alice", "new-password", "student", "A")
    assert app.state.auth_store.principal(token) is None
    assert login(client, "alice", "alice-pass").status_code == 401
    assert login(client, "alice", "new-password").status_code == 200


def test_switch_login_revokes_previous_session(app_client):
    client, app, _ = app_client
    login(client, "alice", "alice-pass")
    previous = client.cookies.get(COOKIE)
    assert login(client, "bob", "bob-pass").status_code == 200
    assert app.state.auth_store.principal(previous) is None
    assert client.get("/api/auth/me").json()["student_id"] == "B"


def test_demo_api_uses_teacher_session(app_client, monkeypatch):
    client, _, _ = app_client
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    demo = importlib.import_module("demo.scripts.online")
    api = demo.DemoAPI(client, 20)
    api.login("teacher", "teacher-pass")
    assert api.clock().lecture == 1
    with pytest.raises(demo.DemoError, match="教师账号"):
        api.login("alice", "alice-pass")
    assert client.get("/api/auth/me").status_code == 401


def test_teacher_goals_and_escalation_keep_the_original_evidence(app_client, monkeypatch):
    client, _, engine = app_client
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    demo = importlib.import_module("demo.scripts.online")
    with Session(engine) as session:
        old = session.get(Evidence, 1).model_dump(mode="json")
        session.add(
            Mastery(
                student_id="A",
                knowledge_point_id="kp",
                state="escalated",
                updated_on=date(2026, 9, 7),
                evidence_ids=[1],
            )
        )
        task = Task(
            type=TaskType.ESCALATION,
            student_id="A",
            knowledge_point_id="kp",
            class_id="cls_a",
            evidence_ids=[1],
            reason="请单独检查换元过程",
            created_on=date(2026, 9, 7),
        )
        session.add(task)
        session.commit()
        task_id = task.id
    assert login(client, "alice", "alice-pass").status_code == 200
    goal = {"knowledge_point_ids": ["kp"], "deadline": "2026-09-10"}
    assert client.post("/api/goals", json=goal).status_code == 403
    assert client.get("/api/escalations").status_code == 403
    assert client.post("/api/tasks/1/takeover", json={}).status_code == 403

    demo.DemoAPI(client, 20).login("teacher", "teacher-pass")
    assert (
        client.post(
            "/api/goals?class_id=cls_a",
            json={**goal, "knowledge_point_ids": ["missing"]},
        ).status_code
        == 422
    )
    created = client.post("/api/goals?class_id=cls_a", json=goal)
    assert created.status_code == 200
    assert client.get("/api/goals?class_id=cls_a").json() == [created.json()]
    assert client.get("/api/class/overview?class_id=cls_a").json()["escalated"] == 1
    escalation = client.get("/api/escalations?class_id=cls_a").json()[0]
    assert escalation["student_name"] == "A"
    assert escalation["suggestion"] == "请单独检查换元过程"
    assert escalation["evidence"] == [old]

    url = f"/api/escalations/{task_id}/resolve"
    assert client.post(url, json={"note": " "}).status_code == 422
    assert client.post(url, json={"note": "已面谈，安排课后答疑"}).json()["status"] == "done"
    assert client.post(url, json={"note": "重复处理"}).status_code == 409
    resolved = client.get("/api/escalations?class_id=cls_a").json()[0]
    assert resolved["evidence"][0] == old
    assert len(resolved["evidence"]) == 2
    assert resolved["evidence"][1]["kind"] == "teacher"
    assert "已面谈" in resolved["evidence"][1]["reason"]
    assert client.get("/api/class/overview?class_id=cls_a").json()["escalated"] == 0
    with Session(engine) as session:
        assert session.get(Mastery, ("A", "kp")).state == "escalated"


def test_demo_cli_password_stdin_and_logout(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    cli = importlib.import_module("demo.scripts.__main__")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                json={"role": "teacher"},
                headers={"set-cookie": "truegrasp_session=test; Path=/"},
            )
        assert request.headers["cookie"] == "truegrasp_session=test"
        if request.url.path == "/api/clock":
            return httpx.Response(200, json={"teaching_date": "2026-09-07", "lecture": 1})
        return httpx.Response(204)

    client_type = httpx.Client
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("fake-password\n"))
    assert cli.main(["--username", "teacher", "--password-stdin", "clock"]) == 0
    assert calls == ["/api/auth/login", "/api/clock", "/api/auth/logout"]
    assert "fake-password" not in capsys.readouterr().out


def test_event_visibility(app_client):
    client, app, engine = app_client
    assert login(client, "teacher", "teacher-pass").status_code == 200
    teacher = app.state.auth_store.principal(client.cookies.get(COOKIE))
    assert teacher is not None
    alice = Principal("alice", "student", "A")
    assert visible(TokenDelta(task_id=1, text="private"), alice, engine)
    assert not visible(TokenDelta(task_id=2, text="private"), alice, engine)
    assert not visible(MessageDone(task_id=999), alice, engine)
    assert not visible(
        EvidenceAdded(student_id="B", evidence_id=2, knowledge_point_id="kp"), alice, engine
    )
    assert visible(TokenDelta(task_id=2, text="private"), teacher, engine)


@pytest.mark.anyio
async def test_stream_stops_after_logout(app_client):
    _, app, _ = app_client
    result = app.state.auth_store.login("alice", "alice-pass", 3600)
    token, principal = result
    request = SimpleNamespace(
        cookies={COOKIE: token}, state=SimpleNamespace(principal=principal), app=app
    )
    stream = events(request)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    app.state.auth_store.logout(token)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, timeout=3)


@pytest.mark.anyio
async def test_stream_filters_other_student_before_delivery(app_client):
    _, app, engine = app_client
    token, principal = app.state.auth_store.login("alice", "alice-pass", 3600)
    bus = EventBus()
    local_app = SimpleNamespace(
        state=SimpleNamespace(
            auth_store=app.state.auth_store,
            event_bus=bus,
            orchestrator=SimpleNamespace(engine=engine),
        )
    )
    request = SimpleNamespace(
        cookies={COOKIE: token}, state=SimpleNamespace(principal=principal), app=local_app
    )
    stream = events(request)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    await bus.publish(TokenDelta(task_id=2, text="other"))
    await bus.publish(TokenDelta(task_id=1, text="mine"))
    delivered = await asyncio.wait_for(pending, timeout=2)
    assert delivered.text == "mine"
    await stream.aclose()


@pytest.mark.anyio
async def test_t13_t26_teacher_view_updates_after_student_answer(setup, tmp_path, monkeypatch):
    from app.agents.tutor import create_tutor
    from app.agents.verifier import create_verifier
    from tests.test_tutor_integration import Planner

    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "integration-auth.db"))
    get_settings.cache_clear()
    engine, knowledge, task = setup
    auth_path = tmp_path / "integration-auth.db"
    auth_store = AuthStore(str(auth_path))
    auth_store.put_account("teacher", "teacher-pass", "teacher", None)
    with auth_store.connect() as auth_db:
        teacher_id = auth_db.execute(
            "SELECT teacher_id FROM account WHERE username='teacher'"
        ).fetchone()[0]
    with Session(engine) as db:
        db.add_all(
            [
                Classroom(
                    id="cls_integration",
                    teacher_id=teacher_id,
                    name="集成测试班",
                ),
                Enrollment(class_id="cls_integration", student_id="A"),
                StudentPlacement(student_id="A", teacher_id=teacher_id),
            ]
        )
        db.flush()
        error = Evidence(
            student_id="A",
            knowledge_point_id=task.knowledge_point_id,
            class_id="cls_integration",
            kind=EvidenceKind.HOMEWORK,
            student_answer="2*sin(3*x)*cos(3*x)",
            is_correct=False,
            judged_by=JudgedBy.TOOL,
            teaching_date=TODAY,
        )
        db.add(error)
        db.flush()
        scoped_task = Task(
            type=TaskType.TUTORING,
            student_id="A",
            knowledge_point_id=task.knowledge_point_id,
            class_id="cls_integration",
            evidence_ids=[error.id],
            created_on=TODAY,
        )
        db.add(scoped_task)
        db.commit()
        task = task.model_copy(
            update={
                "task_id": scoped_task.id,
                "error_evidence_id": error.id,
            }
        )
    model = Model()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "integration-checkpoint.db"),
        orchestration_dependencies={
            "tutor": tutor,
            "knowledge": knowledge,
            "verifier": create_verifier(llm=model, knowledge=knowledge),
            "planner": Planner(),
        },
    )
    async with app.router.lifespan_context(app):
        store = app.state.auth_store
        store.put_account("alice", "alice-pass", "student", "A")
        store.put_account("bob", "bob-pass", "student", "B")
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as teacher,
            AsyncClient(transport=transport, base_url="http://test") as student,
            AsyncClient(transport=transport, base_url="http://test") as other,
        ):
            for client, name, password in (
                (teacher, "teacher", "teacher-pass"),
                (student, "alice", "alice-pass"),
                (other, "bob", "bob-pass"),
            ):
                assert (
                    await client.post(
                        "/api/auth/login", json={"username": name, "password": password}
                    )
                ).status_code == 200
            runtime = app.state.orchestrator
            await runtime.start_task(task.task_id, teaching_date=TODAY)
            before = (await teacher.get(f"/api/tasks/{task.task_id}/diagnosis")).json()
            assert before["probes"][0]["student_answer"] is None
            assert len(before["hypotheses"]) == 3
            assert (await other.get(f"/api/tasks/{task.task_id}/diagnosis")).status_code == 404
            token = teacher.cookies.get(COOKIE)
            principal = store.principal(token)
            request = SimpleNamespace(
                cookies={COOKIE: token}, state=SimpleNamespace(principal=principal), app=app
            )
            stream = events(request)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)
            target = await runtime.get_answer_target(task.task_id)
            response = await student.post(
                f"/api/tasks/{task.task_id}/messages",
                json={"content": "3*cos(3*x)", "answering_item_id": target.item_id},
            )
            assert response.status_code == 202
            await runtime.wait_idle()
            event = await asyncio.wait_for(pending, timeout=5)
            while event.type not in {"hypothesis_updated", "evidence_added", "message_done"}:
                event = await asyncio.wait_for(anext(stream), timeout=5)
            after = (await teacher.get(f"/api/tasks/{task.task_id}/diagnosis")).json()
            assert after["probes"][0]["student_answer"] == "3*cos(3*x)"
            assert any(h["status"] == "refuted" for h in after["hypotheses"])
            assert (
                await teacher.get(f"/api/evidence/{after['error_evidence']['id']}")
            ).status_code == 200
            await stream.aclose()
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_teacher_registration_logs_in_with_new_class_ownership(app_client):
    client, app, _ = app_client
    assert login(client, "alice", "alice-pass").status_code == 200
    previous = client.cookies.get(COOKIE)
    response = client.post(
        "/api/auth/register",
        json={
            "username": "  new-teacher  ",
            "password": "teacher-new-pass",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json() == {"username": "new-teacher", "role": "teacher", "student_id": None}
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert app.state.auth_store.principal(previous) is None
    assert client.get("/api/auth/me").json() == response.json()
    assert client.get("/api/classes").json() == []
    assert client.get("/api/tasks/1").status_code == 404
    principal = app.state.auth_store.principal(client.cookies.get(COOKIE))
    assert principal.teacher_id.startswith("tch_")
    group = client.post("/api/classes", json={"name": "新教师的班级"})
    assert group.status_code == 201, group.text
    assert group.json()["teacher_id"] == principal.teacher_id
    with app.state.auth_store.connect() as db:
        stored = db.execute(
            "SELECT password_hash FROM account WHERE username='new-teacher'"
        ).fetchone()[0]
    assert stored != "teacher-new-pass"
    assert client.post("/api/auth/logout").status_code == 204
    assert login(client, "new-teacher", "teacher-new-pass").status_code == 200
    assert (
        app.state.auth_store.principal(client.cookies.get(COOKIE)).teacher_id
        == principal.teacher_id
    )


def test_teacher_registration_rejects_duplicates_and_invalid_requests(app_client):
    client, app, _ = app_client
    assert login(client, "teacher", "teacher-pass").status_code == 200
    token = client.cookies.get(COOKIE)
    principal = app.state.auth_store.principal(token)
    for username, old_password in (("teacher", "teacher-pass"), ("alice", "alice-pass")):
        response = client.post(
            "/api/auth/register",
            json={
                "username": username,
                "password": "another-password",
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "username_taken"
        assert app.state.auth_store.principal(token) == principal
        assert app.state.auth_store.login(username, old_password, 60) is not None
        assert app.state.auth_store.login(username, "another-password", 60) is None
    body = {"username": "blocked", "password": "new-teacher-pass"}
    assert (
        client.post(
            "/api/auth/register", json=body, headers={"origin": "https://other.test"}
        ).status_code
        == 403
    )
    for invalid in (
        {**body, "password": "short"},
        {**body, "username": "   "},
        {**body, "role": "student"},
    ):
        assert client.post("/api/auth/register", json=invalid).status_code == 422
    assert app.state.auth_store.login("blocked", body["password"], 60) is None
