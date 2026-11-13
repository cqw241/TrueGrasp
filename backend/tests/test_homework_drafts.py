"""OPT-03 定向检查：多格式草稿、个人授权、局部重试与正式账本隔离。"""

import io

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, select

from app.config import get_settings
from app.contracts.entities import Event, Evidence, Item, KnowledgePoint, Student, Task
from app.db import make_engine
from app.homework.models import PageExtraction
from app.homework.storage import DraftRow, interrupt_unfinished
from app.llm.client import LLMOutputError
from app.main import create_app

ANSWER = {
    "stem": "求 sin(3x) 的导数",
    "student_name": "张同学",
    "student_answer": r"\cos(3x)",
    "answer_kind": "expression",
    "knowledge_point_id": "kp",
    "doubts": ["首个符号看不清，请核对原件"],
}
CSV = (
    "student_id,student_name,item_id,stem,correct_answer,student_answer,knowledge_point_id\n"
    "S01,张同学,hw1,求导,3cos(3x),cos(3x),kp\n"
).encode()


class Parser:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    async def generate(self, messages, output, tools=None):
        assert output is PageExtraction
        self.calls.append(messages)
        response = self.responses.pop(0) if self.responses else {"answers": [ANSWER]}
        if isinstance(response, Exception):
            raise response
        return output.model_validate(response)


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("VISION_LLM_API_KEY", "")
    get_settings.cache_clear()
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    app = create_app(engine=engine, checkpoint_path=str(tmp_path / "cp.db"))
    with TestClient(app) as client:
        for username, role, student_id in (
            ("teacher1", "teacher", None),
            ("teacher2", "teacher", None),
            ("student", "student", "S01"),
        ):
            app.state.auth_store.put_account(username, "test-pass", role, student_id)
        with Session(engine) as session:
            session.add(KnowledgePoint(id="kp", name="链式法则", lecture=2, section=1))
            session.commit()
        app.state.homework_parser_llm = Parser()
        login(client)
        yield client, app, engine
    engine.dispose()
    get_settings.cache_clear()


def login(client, username="teacher1"):
    assert (
        client.post(
            "/api/auth/login", json={"username": username, "password": "test-pass"}
        ).status_code
        == 200
    )


def upload(client, files=None, key="upload-1", **data):
    return client.post(
        "/api/homework-drafts",
        headers={"Idempotency-Key": key},
        files=files,
        data=data,
    )


def read(client, response):
    assert response.status_code == 202, response.text
    return client.get(f"/api/homework-drafts/{response.json()['draft_id']}").json()


def image_bytes(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (160, 100), "white").save(output, format=fmt)
    return output.getvalue()


def test_csv_idempotency_identity_and_no_formal_writes(system):
    client, app, engine = system
    files = [("files", ("hw.csv", CSV, "text/csv"))]
    draft = read(client, upload(client, files))
    assert draft["status"] == "review"
    record = draft["records"][0]
    assert record["student_answer"] == "cos(3x)"
    assert record["identity"]["observed_student_id"] == "S01"
    assert record["identity"]["student_id"] is None
    assert record["identity"]["status"] == "unmatched"
    assert record["source_refs"][0]["row"] == 2
    assert draft["original_extraction"]["records"] == draft["records"]
    repeated = read(client, upload(client, files))
    assert repeated["draft_id"] == draft["draft_id"]
    assert len(client.get("/api/homework-drafts").json()) == 1
    assert upload(client, files, title="changed").status_code == 409
    assert not app.state.homework_parser_llm.calls
    with Session(engine) as session:
        for entity in (Student, Item, Evidence, Event, Task):
            assert not session.exec(select(entity)).all()


@pytest.mark.parametrize("fmt,extension", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")])
def test_image_doubts_preserve_original_and_teacher_privacy(system, fmt, extension):
    client, _, _ = system
    raw = image_bytes(fmt)
    draft = read(client, upload(client, [("files", (f"answer.{extension}", raw))]))
    assert draft["records"][0]["student_answer"] == ANSWER["student_answer"]
    assert any(i["code"] == "recognition_uncertain" for i in draft["records"][0]["issues"])
    path = f"/api/homework-drafts/{draft['draft_id']}"
    source = f"{path}/sources/{draft['sources'][0]['source_id']}"
    assert client.get(source).content == raw
    login(client, "teacher2")
    assert client.get("/api/homework-drafts").json() == []
    assert client.get(path).status_code == client.get(source).status_code == 404
    assert (
        client.post(
            f"{path}/parse-retries",
            json={
                "expected_revision": draft["revision"],
                "source_ids": [draft["sources"][0]["source_id"]],
            },
        ).status_code
        == 404
    )
    login(client, "student")
    assert client.get(path).status_code == 403
    assert upload(client, [("files", ("hw.csv", CSV))]).status_code == 403
    client.cookies.clear()
    assert client.get(source).status_code == 401


def test_pdf_partial_failure_retry_and_supplement(system):
    client, app, _ = system
    with pdfium.PdfDocument.new() as pdf:
        pdf.new_page(200, 200).close()
        pdf.new_page(200, 200).close()
        buffer = io.BytesIO()
        pdf.save(buffer)
    parser = Parser([{"answers": [ANSWER]}, LLMOutputError("provider failed")])
    app.state.homework_parser_llm = parser
    draft = read(client, upload(client, [("files", ("hw.pdf", buffer.getvalue()))]))
    assert draft["status"] == "review"
    assert draft["sources"][0]["page_count"] == 2
    assert len(draft["records"]) == 1
    assert draft["records"][0]["source_refs"][0]["page"] == 1
    assert draft["parse_errors"][0]["page"] == 2
    path = f"/api/homework-drafts/{draft['draft_id']}"
    first_id = draft["records"][0]["record_id"]
    original = draft["original_extraction"]
    body = {
        "expected_revision": draft["revision"],
        "source_ids": [draft["sources"][0]["source_id"]],
    }
    response = client.post(f"{path}/parse-retries", json=body)
    retried = read(client, response)
    assert len(parser.calls) == 3  # 只重新调用失败页，成功页不重跑。
    assert retried["parse_errors"] == []
    assert len(retried["records"]) == 2
    assert retried["records"][0]["record_id"] == first_id
    assert retried["original_extraction"] == original
    assert client.post(f"{path}/parse-retries", json=body).status_code in (409, 422)
    response = client.post(
        f"{path}/sources",
        data={"expected_revision": retried["revision"]},
        files=[("files", ("supplement.png", image_bytes()))],
    )
    supplemented = read(client, response)
    assert len(supplemented["sources"]) == 2
    assert len(supplemented["records"]) == 3
    assert supplemented["records"][0]["record_id"] == first_id


def test_general_csv_and_text_extract_without_inventing_student(system):
    client, app, _ = system
    draft = read(
        client, upload(client, [("files", ("names.csv", "姓名,作答\n张同学,不清楚".encode()))])
    )
    assert len(app.state.homework_parser_llm.calls) == 1
    assert draft["records"][0]["identity"]["candidate_student_ids"] == []
    draft = read(
        client, upload(client, key="text", prompt_text="题目：证明连续性。作答：由定义可知。")
    )
    assert draft["sources"][0]["kind"] == "text"
    del app.state.homework_parser_llm
    failed = read(client, upload(client, key="unconfigured", prompt_text="求导"))
    assert failed["status"] == "parse_failed"
    assert "配置" in failed["parse_errors"][0]["message"]


def test_bad_files_limits_and_restart_recovery(system):
    client, _, engine = system
    assert upload(client, [("files", ("bad.exe", b"data"))]).status_code == 415
    assert upload(client, [("files", ("bad.png", b"not image"))]).status_code == 422
    assert upload(client, [("files", ("empty.csv", b""))]).status_code == 422
    assert upload(client, [("files", ("big.png", b"x" * (8 * 1024 * 1024 + 1)))]).status_code == 413
    assert upload(client, [("files", ("bad.pdf", b"not pdf"))]).status_code == 415
    draft = read(client, upload(client, [("files", ("hw.csv", CSV))]))
    with Session(engine) as session:
        row = session.get(DraftRow, draft["draft_id"])
        row.payload = {**row.payload, "status": "parsing"}
        session.add(row)
        session.commit()
    interrupt_unfinished(engine)
    recovered = client.get(f"/api/homework-drafts/{draft['draft_id']}").json()
    assert recovered["status"] == "review"
    assert recovered["records"] == draft["records"]
