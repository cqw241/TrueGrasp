"""独立认证库、会话和 API 权限。"""

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyCookie
from sqlmodel import Session, select

from app.classroom import Classroom, Enrollment, can_see, may_work, owned_classes
from app.config import get_settings
from app.contracts.entities import Evidence, Misconception, Task

COOKIE = "truegrasp_session"
cookie_scheme = APIKeyCookie(name=COOKIE, auto_error=False)


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    student_id: str | None
    teacher_id: str | None = None


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split(":", 1)
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), stored)
    except (ValueError, TypeError):
        return False


class AuthStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('teacher','student')),
                    student_id TEXT, teacher_id TEXT
                );
                CREATE TABLE IF NOT EXISTS auth_session (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    expires REAL NOT NULL
                );
            """)

            db.execute("BEGIN IMMEDIATE")
            if "teacher_id" not in {row[1] for row in db.execute("PRAGMA table_info(account)")}:
                db.execute("ALTER TABLE account ADD COLUMN teacher_id TEXT")
            db.execute(
                "UPDATE account SET teacher_id='tch_' || lower(hex(randomblob(16))) "
                "WHERE role='teacher' AND teacher_id IS NULL"
            )
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_teacher_id ON account(teacher_id)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_student_account ON account(student_id)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_account(self, username: str, password: str, role: str, student_id: str | None) -> None:
        if not username.strip() or not password or role not in {"teacher", "student"}:
            raise ValueError("账号、密码和角色不能为空")
        if (role == "student") != bool(student_id):
            raise ValueError("学生账号必须绑定学生 ID，教师账号不得绑定")
        hashed = password_hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT role, student_id FROM account WHERE username=?", (username,)
            ).fetchone()
            if old is not None and old != (role, student_id):
                raise ValueError("不能修改现有账号的角色或绑定身份")
            if (
                student_id
                and db.execute(
                    "SELECT 1 FROM account WHERE student_id=? AND username!=?",
                    (student_id, username),
                ).fetchone()
            ):
                raise ValueError("该学生已绑定其他账号")
            db.execute(
                "INSERT INTO account(username,password_hash,role,student_id,teacher_id) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
                "password_hash=excluded.password_hash",
                (
                    username,
                    hashed,
                    role,
                    student_id,
                    "tch_" + secrets.token_hex(16) if role == "teacher" else None,
                ),
            )
            db.execute("DELETE FROM auth_session WHERE username=?", (username,))

    def login(self, username: str, password: str, seconds: int) -> tuple[str, Principal] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT password_hash, role, student_id, teacher_id FROM account WHERE username=? "
                "AND (role='teacher' OR (SELECT count(*) FROM account b WHERE "
                "b.student_id=account.student_id)=1)",
                (username,),
            ).fetchone()
            if row is None or not verify_password(password, row[0]):
                return None
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM auth_session WHERE expires <= ?", (time.time(),))
            db.execute(
                "INSERT INTO auth_session VALUES (?, ?, ?)",
                (self.token_hash(token), username, time.time() + seconds),
            )
            return token, Principal(username, row[1], row[2], row[3])

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def principal(self, token: str | None) -> Principal | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT a.username, a.role, a.student_id, a.teacher_id FROM auth_session s "
                "JOIN account a ON a.username=s.username WHERE s.token_hash=? AND s.expires>? "
                "AND (a.role='teacher' OR "
                "(SELECT count(*) FROM account b WHERE b.student_id=a.student_id)=1)",
                (self.token_hash(token), time.time()),
            ).fetchone()
        return Principal(*row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM auth_session WHERE token_hash=?", (self.token_hash(token),))


def business_error(code: str, message: str) -> dict:
    return {"code": code, "message": message, "retryable": False, "issues": []}


def check_origin(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "请求来源不允许")
    origin = request.headers.get("origin")
    expected = get_settings().auth_public_origin or str(request.base_url).rstrip("/")
    if origin and origin.rstrip("/") != expected.rstrip("/"):
        raise HTTPException(403, "请求来源不允许")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(403, "请求来源不允许")


def current_principal(request: Request) -> Principal:
    token = request.cookies.get(COOKIE)
    principal = request.app.state.auth_store.principal(token)
    if principal is None:
        raise HTTPException(401, "请先登录")
    return principal


def authorize(request: Request, _cookie: Annotated[str | None, Security(cookie_scheme)]) -> None:
    """默认只允许教师；学生必须明确列入共享接口并检查对象归属。"""
    try:
        check_origin(request)
        principal = current_principal(request)
    except HTTPException as exc:
        if request.scope["route"].path.startswith("/classes"):
            code = "login_required" if exc.status_code == 401 else "origin_forbidden"
            raise HTTPException(exc.status_code, business_error(code, exc.detail)) from exc
        raise
    request.state.principal = principal
    route = request.scope["route"].path
    method = request.method
    params = request.path_params
    if principal.role == "teacher":
        with Session(request.app.state.orchestrator.engine) as session:
            if "task_id" in params:
                task = session.get(Task, int(params["task_id"]))
                if not can_see(session, principal, task):
                    raise HTTPException(404, "任务不存在")
                if method != "GET" and not may_work(session, task):
                    raise HTTPException(409, "学生已退出任务所属班级")
            if "evidence_id" in params and not can_see(
                session, principal, session.get(Evidence, int(params["evidence_id"]))
            ):
                raise HTTPException(404, "证据不存在")
            if "misconception_id" in params:
                cause = session.get(Misconception, params["misconception_id"])
                if (
                    cause is None
                    or cause.class_id is None
                    or session.exec(
                        owned_classes(principal.teacher_id).where(Classroom.id == cause.class_id)
                    ).first()
                    is None
                ):
                    raise HTTPException(404, "错因不存在")
            if "student_id" in params and "class_id" not in params:
                # Former teachers can still read the student's records from their
                # own classes. Only an active enrollment allows new work.
                query = select(Enrollment).where(
                    Enrollment.student_id == params["student_id"],
                    Enrollment.class_id.in_(owned_classes(principal.teacher_id)),
                )
                if method != "GET":
                    query = query.where(Enrollment.status == "active")
                if session.exec(query).first() is None:
                    raise HTTPException(404, "学生不存在")
        return
    if method == "GET" and route in {
        "/events",
        "/students",
        "/tasks",
        "/knowledge/points",
        "/knowledge/path",
    }:
        if route == "/tasks":
            requested = request.query_params.get("student_id")
            if requested and requested != principal.student_id:
                raise HTTPException(403, "无权访问其他学生的数据")
        return
    student_routes = {
        ("GET", "/students/{student_id}/timeline"),
        ("GET", "/students/{student_id}/mastery"),
        ("GET", "/students/{student_id}/verification"),
        ("POST", "/students/{student_id}/question-photo"),
        ("POST", "/students/{student_id}/questions"),
    }
    if (method, route) in student_routes:
        if params["student_id"] != principal.student_id:
            raise HTTPException(403, "无权访问其他学生的数据")
        return
    task_routes = {
        ("GET", "/tasks/{task_id}"),
        ("GET", "/tasks/{task_id}/messages"),
        ("GET", "/tasks/{task_id}/answer-target"),
        ("GET", "/tasks/{task_id}/diagnosis"),
        ("POST", "/tasks/{task_id}/start"),
        ("POST", "/tasks/{task_id}/messages"),
        ("POST", "/tasks/{task_id}/answer-photo"),
        ("POST", "/tasks/{task_id}/question-photo"),
    }
    if (method, route) in task_routes:
        with Session(request.app.state.orchestrator.engine) as session:
            task = session.get(Task, int(params["task_id"]))
            if not can_see(session, principal, task):
                raise HTTPException(404, "任务不存在")
            if method != "GET" and not may_work(session, task):
                raise HTTPException(409, "学生已退出任务所属班级")
        return
    if route == "/evidence/{evidence_id}" and method == "GET":
        with Session(request.app.state.orchestrator.engine) as session:
            if not can_see(session, principal, session.get(Evidence, int(params["evidence_id"]))):
                raise HTTPException(404, "证据不存在")
        return
    if route.startswith("/classes"):
        raise HTTPException(403, business_error("teacher_required", "需要教师权限"))
    raise HTTPException(403, "需要教师权限")
