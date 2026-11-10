"""认证、对象授权和事件隔离验收。"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session

from app.api.stream import events, visible
from app.auth import COOKIE, Principal
from app.config import get_settings
from app.contracts.entities import Evidence, KnowledgePoint, Student, Task
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
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    today = date(2026, 9, 7)
    with Session(engine) as db:
        db.add_all([Student(id="A", name="A"), Student(id="B", name="B")])
        db.add(KnowledgePoint(id="kp", name="KP", lecture=1, section=1))
        db.flush()
        db.add_all(
            [
                Task(
                    id=1,
                    type=TaskType.TUTORING,
                    student_id="A",
                    knowledge_point_id="kp",
                    created_on=today,
                ),
                Task(
                    id=2,
                    type=TaskType.TUTORING,
                    student_id="B",
                    knowledge_point_id="kp",
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
                    kind=EvidenceKind.HOMEWORK,
                    judged_by=JudgedBy.TOOL,
                    teaching_date=today,
                ),
                Evidence(
                    id=2,
                    student_id="B",
                    knowledge_point_id="kp",
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
        store.put_account("teacher", "teacher-pass", "teacher", None)
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


def test_event_visibility(app_client):
    _, _, engine = app_client
    alice = Principal("alice", "student", "A")
    teacher = Principal("teacher", "teacher", None)
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
        store.put_account("teacher", "teacher-pass", "teacher", None)
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
