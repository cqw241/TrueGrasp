"""OPT-04: three focused checks for review, atomic publication and basic access."""

from sqlmodel import Session, select

from app.contracts.entities import Event, Evidence, Task
from app.homework.storage import PublicationRow, PublishedRecord
from app.ingest.models import HomeworkAnalysis
from app.orchestration.storage import PendingWork
from tests.test_homework_drafts import image_bytes, login, read, upload
from tests.test_homework_drafts import system as draft_fixture

review_system = draft_fixture


class Analysis:
    def __init__(self):
        self.calls = 0

    async def generate(self, messages, output, tools=None):
        self.calls += 1
        assert output is HomeworkAnalysis
        return output(
            knowledge_point_id="kp",
            is_correct=False,
            misconception_id=None,
            reason="原作答漏乘内层导数",
        )


def setup_review(client):
    group = client.post("/api/classes", json={"name": "演示班"}).json()
    member = client.post(
        f"/api/classes/{group['class_id']}/students",
        json={"name": "张同学", "student_number": "202601"},
    ).json()
    draft = read(client, upload(client, prompt_text="求 sin(3x) 的导数，张同学作答 cos(3x)"))
    path = f"/api/homework-drafts/{draft['draft_id']}"
    bound = client.patch(
        path, json={"expected_revision": draft["revision"], "class_id": group["class_id"]}
    )
    assert bound.status_code == 200, bound.text
    return path, bound.json(), group, member


def confirm(client, path, draft, member):
    q, r = draft["questions"][0], draft["records"][0]
    response = client.patch(
        path,
        json={
            "expected_revision": draft["revision"],
            "question_updates": [
                {
                    "question_id": q["question_id"],
                    "stem": "求 sin(3x) 的一阶导数",
                    "correct_answer": "3cos(3x)",
                    "knowledge_point_id": "kp",
                }
            ],
            "record_updates": [
                {
                    "record_id": r["record_id"],
                    "student_id": member["student_id"],
                    "student_answer": "cos(3x)",
                    "review_status": "confirmed",
                }
            ],
            "issue_resolutions": [
                {"issue_id": i["issue_id"], "action": "accepted"}
                for i in r["issues"]
                if i["code"] == "recognition_uncertain"
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_review_keeps_original_and_resets_shared_confirmation(review_system):
    client, _, _ = review_system
    path, draft, group, member = setup_review(client)
    original = draft["original_extraction"]
    assert draft["records"][0]["identity"]["status"] == "proposed"
    reviewed = confirm(client, path, draft, member)
    assert reviewed["original_extraction"] == original
    assert reviewed["records"][0]["student_answer"] == "cos(3x)"
    assert reviewed["records"][0]["review_status"] == "confirmed"
    assert reviewed["edits"]
    changed = client.patch(
        path,
        json={
            "expected_revision": reviewed["revision"],
            "question_updates": [
                {
                    "question_id": reviewed["questions"][0]["question_id"],
                    "correct_answer": "3*cos(3*x)",
                }
            ],
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["records"][0]["review_status"] == "pending"
    other = client.post(
        f"/api/classes/{group['class_id']}/students", json={"name": "李同学"}
    ).json()
    manual = client.patch(
        path,
        json={
            "expected_revision": changed.json()["revision"],
            "record_updates": [
                {
                    "record_id": reviewed["records"][0]["record_id"],
                    "student_id": other["student_id"],
                }
            ],
        },
    ).json()
    supplemented = read(
        client,
        client.post(
            path + "/sources",
            data={"expected_revision": manual["revision"]},
            files=[("files", ("more.png", image_bytes()))],
        ),
    )
    assert supplemented["records"][0]["identity"]["student_id"] == other["student_id"]
    assert supplemented["original_extraction"] == original
    assert client.patch(path, json={"expected_revision": reviewed["revision"]}).status_code == 409


def test_publish_is_atomic_and_replays_without_duplicate_work(review_system):
    client, app, engine = review_system
    path, draft, group, member = setup_review(client)
    reviewed = confirm(client, path, draft, member)
    model = Analysis()
    app.state.homework_llm = model
    body = {
        "expected_revision": reviewed["revision"],
        "record_ids": [reviewed["records"][0]["record_id"]],
    }
    assert client.post(path + "/publish-preview", json=body).json()["can_publish"]
    with Session(engine) as session:
        for entity in (Evidence, Event, Task):
            assert not session.exec(select(entity)).all()
    response = client.post(path + "/publish", json=body, headers={"Idempotency-Key": "publish1"})
    assert response.status_code == 201, response.text
    publication = response.json()
    assert publication["status"] == "published"
    assert publication["confirmed_snapshot"]["records"][0]["student_answer"] == "cos(3x)"
    replay = client.post(path + "/publish", json=body, headers={"Idempotency-Key": "publish1"})
    assert replay.status_code == 200, replay.text
    assert replay.json()["publication_id"] == publication["publication_id"]
    assert model.calls == 1
    latest = client.get(path).json()
    assert latest["status"] == "published"
    assert (
        client.post(
            path + "/publish",
            json={**body, "expected_revision": latest["revision"]},
            headers={"Idempotency-Key": "publish2"},
        ).status_code
        == 409
    )
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.class_id == group["class_id"]
        assert evidence.student_id == member["student_id"]
        assert evidence.student_answer == "cos(3x)"
        assert len(session.exec(select(Event)).all()) == 1
        assert session.exec(select(PublishedRecord)).one().evidence_id == evidence.id
        assert session.exec(select(PublicationRow)).one().status == "published"
        assert (
            session.exec(select(PendingWork)).one().key
            == f"event:{publication['import_result']['event_id']}"
        )
    assert client.get(response.headers["Location"]).json()["workflow"]["state"] == "pending"
    assert (
        client.patch(
            path,
            json={
                "expected_revision": latest["revision"],
                "record_updates": [
                    {"record_id": body["record_ids"][0], "student_answer": "edited"}
                ],
            },
        ).status_code
        == 409
    )


def test_incomplete_review_and_other_teacher_are_rejected(review_system):
    client, _, engine = review_system
    path, draft, group, _ = setup_review(client)
    body = {
        "expected_revision": draft["revision"],
        "record_ids": [draft["records"][0]["record_id"]],
    }
    preview = client.post(path + "/publish-preview", json=body).json()
    assert not preview["can_publish"]
    assert preview["blocking_issues"]
    assert (
        client.post(path + "/publish", json=body, headers={"Idempotency-Key": "bad"}).status_code
        == 422
    )
    login(client, "teacher2")
    assert client.get(path).status_code == 404
    assert client.patch(path, json={"expected_revision": draft["revision"]}).status_code == 404
    assert (
        client.post(path + "/publish", json=body, headers={"Idempotency-Key": "bad"}).status_code
        == 404
    )
    assert (
        client.get("/api/homework-drafts", params={"class_id": group["class_id"]}).status_code
        == 404
    )
    with Session(engine) as session:
        assert not session.exec(select(Evidence)).all()
        assert not session.exec(select(PublicationRow)).all()
