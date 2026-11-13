"""OPT-01/02 acceptance: class ownership, rosters, credentials and isolation."""

import hashlib
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.stream import visible
from app.auth import COOKIE, verify_password
from app.class_migration import migrate_student
from app.classroom import ClassMastery, Enrollment, StudentPlacement
from app.config import get_settings
from app.contracts.entities import Event, Evidence, Mastery, Student, Task
from app.contracts.enums import (
    EventType,
    EvidenceKind,
    JudgedBy,
    MasteryState,
    TaskStatus,
    TaskType,
)
from app.contracts.events import EvidenceAdded, TokenDelta
from app.contracts.values import PENDING_KNOWLEDGE_POINT_ID
from app.db import init_db, make_engine
from app.main import create_app
from app.orchestration.storage import PendingWork, TeacherTaskControl


@pytest.fixture
def class_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "auth.db"))
    get_settings.cache_clear()
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    app = create_app(engine=engine, checkpoint_path=str(tmp_path / "checkpoint.db"))
    with TestClient(app) as client:
        store = app.state.auth_store
        store.put_account("teacher-a", "teacher-a-pass", "teacher", None)
        store.put_account("teacher-b", "teacher-b-pass", "teacher", None)
        yield client, app, engine, store
    engine.dispose()
    get_settings.cache_clear()


def login(client, username, password):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response


def create_class(client, name):
    response = client.post("/api/classes", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def add_student(client, class_id, name, number):
    response = client.post(
        f"/api/classes/{class_id}/students",
        json={"name": name, "student_number": number},
    )
    assert response.status_code == 201, response.text
    return response.json()


def issue_setup(client, class_id, student_id):
    response = client.post(f"/api/classes/{class_id}/students/{student_id}/setup-link")
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, payload["setup_path"].rsplit("#", 1)[1]


def teacher_id(store, username):
    with store.connect() as db:
        return db.execute(
            "SELECT teacher_id FROM account WHERE username=?",
            (username,),
        ).fetchone()[0]


def test_class_ownership_and_role_isolation(class_app):
    client, app, engine, store = class_app
    login(client, "teacher-a", "teacher-a-pass")
    class_a = create_class(client, "高等数学 A 班")
    assert class_a["teacher_id"] == teacher_id(store, "teacher-a")
    student_a = add_student(client, class_a["class_id"], "甲同学", "A001")

    login(client, "teacher-b", "teacher-b-pass")
    class_b = create_class(client, "高等数学 B 班")
    assert class_b["teacher_id"] == teacher_id(store, "teacher-b")
    assert class_a["teacher_id"] != class_b["teacher_id"]
    student_b = add_student(client, class_b["class_id"], "乙同学", "B001")

    assert client.get("/api/classes").json() == [class_b]
    assert client.get(f"/api/classes/{class_a['class_id']}/students").status_code == 404
    assert (
        client.patch(
            f"/api/classes/{class_a['class_id']}",
            json={"name": "越权改名"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/classes/{class_a['class_id']}/students",
            json={"name": "越权学生", "student_number": "X001"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/classes/{class_a['class_id']}/students/{student_a['student_id']}"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/classes/{class_a['class_id']}/students/{student_a['student_id']}/setup-link"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/classes/{class_b['class_id']}/students",
            json={"student_id": student_a["student_id"]},
        ).status_code
        == 404
    )

    login(client, "teacher-a", "teacher-a-pass")
    assert client.get("/api/classes").json() == [class_a]
    assert (
        client.post(
            f"/api/classes/{class_a['class_id']}/students",
            json={"student_id": student_b["student_id"]},
        ).status_code
        == 404
    )
    assert client.get("/api/classes/available-students").json() == []

    store.put_account("student-a", "student-a-pass", "student", student_a["student_id"])
    login(client, "student-a", "student-a-pass")
    assert client.get("/api/classes").status_code == 403
    assert client.get("/api/classes/available-students").status_code == 403

    with Session(engine) as session:
        assert (
            session.exec(select(Student).where(Student.id == student_a["student_id"])).one().name
            == "甲同学"
        )


def test_roster_import_reports_conflicts_and_preserves_history(class_app):
    client, _, engine, _ = class_app
    login(client, "teacher-a", "teacher-a-pass")
    class_a = create_class(client, "A 班")
    class_b = create_class(client, "B 班")
    student = add_student(client, class_a["class_id"], "甲同学", "001")

    duplicate_number = client.post(
        f"/api/classes/{class_a['class_id']}/students",
        json={"name": "同名不同人", "student_number": "001"},
    )
    assert duplicate_number.status_code == 409
    assert duplicate_number.json()["detail"]["code"] == "student_number_conflict"

    duplicate_member = client.post(
        f"/api/classes/{class_a['class_id']}/students",
        json={"student_id": student["student_id"], "student_number": "001"},
    )
    assert duplicate_member.status_code == 409
    assert duplicate_member.json()["detail"]["code"] == "duplicate_member"

    active_class_conflict = client.post(
        f"/api/classes/{class_b['class_id']}/students",
        json={"student_id": student["student_id"]},
    )
    assert active_class_conflict.status_code == 409
    assert active_class_conflict.json()["detail"]["code"] == "active_class_conflict"

    imported = client.post(
        f"/api/classes/{class_a['class_id']}/students/import",
        json={
            "rows": [
                ["乙同学", "002"],
                ["重复学号", "002"],
                [""],
                ["字段过多", "003", "extra"],
                ["缺少学号"],
            ]
        },
    )
    assert imported.status_code == 200, imported.text
    assert [row["status"] for row in imported.json()] == [
        "added",
        "duplicate",
        "missing",
        "failed",
        "missing",
    ]
    roster = client.get(f"/api/classes/{class_a['class_id']}/students").json()
    assert {row["student_number"] for row in roster} == {"001", "002"}

    with Session(engine) as session:
        task = Task(
            type=TaskType.TUTORING,
            student_id=student["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            class_id=class_a["class_id"],
            status=TaskStatus.PENDING,
            created_on=date(2026, 11, 13),
        )
        session.add(task)
        session.flush()
        session.add(
            PendingWork(
                key=f"task:{task.id}",
                payload={"task_id": task.id},
                pending=True,
            )
        )
        session.add(
            TeacherTaskControl(
                task_id=task.id,
                taken_over=True,
                overridden=False,
                note="保留控制记录",
            )
        )
        evidence = Evidence(
            student_id=student["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            class_id=class_a["class_id"],
            kind=EvidenceKind.HOMEWORK,
            judged_by=JudgedBy.TOOL,
            teaching_date=date(2026, 11, 13),
        )
        session.add(evidence)
        session.commit()
        task_id = task.id
        evidence_id = evidence.id

    assert (
        client.delete(
            f"/api/classes/{class_a['class_id']}/students/{student['student_id']}"
        ).status_code
        == 204
    )
    assert (
        client.delete(
            f"/api/classes/{class_a['class_id']}/students/{student['student_id']}"
        ).status_code
        == 204
    )
    active = client.get(f"/api/classes/{class_a['class_id']}/students").json()
    assert student["student_id"] not in {row["student_id"] for row in active}
    left = client.get(f"/api/classes/{class_a['class_id']}/students?include_left=true").json()
    assert (
        next(row for row in left if row["student_id"] == student["student_id"])["status"] == "left"
    )

    with Session(engine) as session:
        assert session.get(Student, student["student_id"]) is not None
        assert session.get(Evidence, evidence_id) is not None
        assert session.get(Task, task_id).status == TaskStatus.CANCELLED
        assert session.get(PendingWork, f"task:{task_id}").pending is False
        assert session.get(TeacherTaskControl, task_id).taken_over is False
        assert (
            session.get(Enrollment, (class_a["class_id"], student["student_id"])).status == "left"
        )
        assert session.get(StudentPlacement, student["student_id"]) is not None

    assert (
        client.post(
            f"/api/classes/{class_b['class_id']}/students",
            json={"student_id": student["student_id"]},
        ).status_code
        == 201
    )


def test_student_setup_reset_and_password_storage(class_app):
    client, app, engine, store = class_app
    login(client, "teacher-a", "teacher-a-pass")
    group = create_class(client, "设置密码班")
    student = add_student(client, group["class_id"], "甲同学", "001")
    setup, token = issue_setup(client, group["class_id"], student["student_id"])
    assert setup["username"]

    with store.connect() as db:
        row = db.execute(
            "SELECT token_hash FROM student_setup WHERE student_id=?",
            (student["student_id"],),
        ).fetchone()
        assert row[0] == hashlib.sha256(token.encode()).hexdigest()
        assert row[0] != token

    assert (
        client.post(
            "/api/auth/student-setup",
            json={"token": token, "password": "initial-password"},
            headers={"origin": "https://evil.test"},
        ).status_code
        == 403
    )
    first_password = "first-password-123"
    completed = client.post(
        "/api/auth/student-setup",
        json={"token": token, "password": first_password},
    )
    assert completed.status_code == 200, completed.text
    assert completed.headers["cache-control"] == "no-store"
    assert completed.json()["username"] == setup["username"]
    assert (
        client.post(
            "/api/auth/student-setup",
            json={"token": token, "password": "another-password-123"},
        ).status_code
        == 404
    )

    with store.connect() as db:
        stored = db.execute(
            "SELECT password_hash FROM account WHERE username=?",
            (setup["username"],),
        ).fetchone()[0]
        assert first_password not in stored
        assert verify_password(first_password, stored)

    login(client, setup["username"], first_password)
    me = client.get("/api/auth/me").json()
    assert me["student_id"] == student["student_id"]
    assert client.get("/api/students").json()[0]["id"] == student["student_id"]

    with Session(engine) as session:
        own_task = Task(
            type=TaskType.TUTORING,
            student_id=student["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            class_id=group["class_id"],
            created_on=date(2026, 11, 13),
        )
        session.add(own_task)
        session.commit()
        own_task_id = own_task.id
    assert [task["id"] for task in client.get("/api/tasks").json()] == [own_task_id]
    assert client.get("/api/tasks/9999").status_code == 404

    login(client, "teacher-a", "teacher-a-pass")
    reset, reset_token = issue_setup(client, group["class_id"], student["student_id"])
    assert reset["username"] == setup["username"]
    second_password = "second-password-456"
    assert (
        client.post(
            "/api/auth/student-setup",
            json={"token": reset_token, "password": second_password},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": setup["username"], "password": first_password},
        ).status_code
        == 401
    )
    login(client, setup["username"], second_password)


def test_tasks_evidence_and_sse_are_class_scoped(class_app):
    client, app, engine, store = class_app
    login(client, "teacher-a", "teacher-a-pass")
    class_a = create_class(client, "A 班")
    student_a = add_student(client, class_a["class_id"], "甲同学", "A001")
    login(client, "teacher-b", "teacher-b-pass")
    class_b = create_class(client, "B 班")
    student_b = add_student(client, class_b["class_id"], "乙同学", "B001")

    with Session(engine) as session:
        task_a = Task(
            type=TaskType.TUTORING,
            student_id=student_a["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            class_id=class_a["class_id"],
            created_on=date(2026, 11, 13),
        )
        task_b = Task(
            type=TaskType.TUTORING,
            student_id=student_b["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            class_id=class_b["class_id"],
            created_on=date(2026, 11, 13),
        )
        unassigned = Task(
            type=TaskType.TUTORING,
            student_id=student_a["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            created_on=date(2026, 11, 13),
        )
        session.add_all([task_a, task_b, unassigned])
        session.flush()
        evidence_a = Evidence(
            student_id=student_a["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            class_id=class_a["class_id"],
            kind=EvidenceKind.HOMEWORK,
            judged_by=JudgedBy.TOOL,
            teaching_date=date(2026, 11, 13),
        )
        evidence_b = Evidence(
            student_id=student_b["student_id"],
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            class_id=class_b["class_id"],
            kind=EvidenceKind.HOMEWORK,
            judged_by=JudgedBy.TOOL,
            teaching_date=date(2026, 11, 13),
        )
        session.add_all([evidence_a, evidence_b])
        session.commit()
        task_a_id, task_b_id = task_a.id, task_b.id
        unassigned_task_id = unassigned.id
        evidence_a_id, evidence_b_id = evidence_a.id, evidence_b.id

    login(client, "teacher-a", "teacher-a-pass")
    assert [row["id"] for row in client.get("/api/tasks").json()] == [task_a_id]
    assert client.get(f"/api/tasks/{task_b_id}").status_code == 404
    assert client.get(f"/api/evidence/{evidence_b_id}").status_code == 404
    assert client.get(f"/api/evidence/{evidence_a_id}").status_code == 200
    assert client.get(f"/api/class/overview?class_id={class_b['class_id']}").status_code == 404
    assert client.get(f"/api/students/{student_b['student_id']}/timeline").status_code == 404
    assert (
        client.patch(
            f"/api/tasks/{task_b_id}",
            json={"cancel": True},
        ).status_code
        == 404
    )

    teacher_a = store.principal(client.cookies.get(COOKIE))
    teacher_b_result = store.login("teacher-b", "teacher-b-pass", 3600)
    assert teacher_b_result is not None
    teacher_b = teacher_b_result[1]
    assert visible(TokenDelta(task_id=task_a_id, text="A"), teacher_a, engine)
    assert not visible(TokenDelta(task_id=task_b_id, text="B"), teacher_a, engine)
    assert not visible(
        TokenDelta(task_id=unassigned_task_id, text="legacy"),
        teacher_a,
        engine,
    )
    assert visible(TokenDelta(task_id=task_b_id, text="B"), teacher_b, engine)
    assert not visible(
        EvidenceAdded(
            student_id=student_b["student_id"],
            evidence_id=evidence_b_id,
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
        ),
        teacher_a,
        engine,
    )


def test_teacher_identity_is_stable_when_password_changes(class_app):
    _, _, _, store = class_app
    first = teacher_id(store, "teacher-a")
    store.put_account("teacher-a", "replacement-password", "teacher", None)
    assert teacher_id(store, "teacher-a") == first
    assert store.login("teacher-a", "replacement-password", 3600)[1].teacher_id == first


def test_legacy_student_migration_is_explicit_and_preserves_ledger(class_app):
    client, _, engine, _ = class_app
    login(client, "teacher-a", "teacher-a-pass")
    group = create_class(client, "历史数据班")

    with Session(engine) as session:
        session.add(Student(id="legacy-a", name="历史学生"))
        session.flush()
        task = Task(
            type=TaskType.TUTORING,
            student_id="legacy-a",
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            created_on=date(2026, 11, 13),
        )
        session.add(task)
        session.flush()
        evidence = Evidence(
            student_id="legacy-a",
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            task_id=task.id,
            kind=EvidenceKind.HOMEWORK,
            judged_by=JudgedBy.TOOL,
            teaching_date=date(2026, 11, 13),
        )
        event = Event(
            type=EventType.HOMEWORK_IMPORTED,
            student_id="legacy-a",
            payload={},
            teaching_date=date(2026, 11, 13),
        )
        mastery = Mastery(
            student_id="legacy-a",
            knowledge_point_id=PENDING_KNOWLEDGE_POINT_ID,
            state=MasteryState.TUTORING,
            evidence_ids=[],
            updated_on=date(2026, 11, 13),
        )
        session.add_all([evidence, event, mastery])
        session.commit()
        task_id, evidence_id = task.id, evidence.id

    result = migrate_student(engine, group["class_id"], "legacy-a")
    assert result["tasks"] == 1
    assert result["evidence"] == 1
    assert result["events"] == 1
    assert result["mastery_rows"] == 1

    with Session(engine) as session:
        assert session.get(Enrollment, (group["class_id"], "legacy-a")).status == "active"
        assert session.get(StudentPlacement, "legacy-a").teacher_id == group["teacher_id"]
        assert session.get(Task, task_id).class_id == group["class_id"]
        assert session.get(Evidence, evidence_id).class_id == group["class_id"]
        assert (
            session.get(
                ClassMastery,
                (group["class_id"], "legacy-a", PENDING_KNOWLEDGE_POINT_ID),
            ).state
            == MasteryState.TUTORING
        )

    assert [row["id"] for row in client.get("/api/tasks").json()] == [task_id]
    assert client.get(f"/api/evidence/{evidence_id}").status_code == 200

    with Session(engine) as session:
        evidence = session.get(Evidence, evidence_id)
        evidence.reason = "不允许修改"
        with pytest.raises(IntegrityError):
            session.commit()

    with pytest.raises(ValueError, match="另一个有效班级"):
        login(client, "teacher-b", "teacher-b-pass")
        other = create_class(client, "另一班")
        migrate_student(engine, other["class_id"], "legacy-a")
