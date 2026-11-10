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
from sqlmodel import Session

from app.config import get_settings
from app.contracts.entities import Evidence, Task

COOKIE = "truegrasp_session"
cookie_scheme = APIKeyCookie(name=COOKIE, auto_error=False)


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    student_id: str | None


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
                    student_id TEXT
                );
                CREATE TABLE IF NOT EXISTS auth_session (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    expires REAL NOT NULL
                );
            """)

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
        with self.connect() as db:
            db.execute(
                "INSERT INTO account VALUES (?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
                "password_hash=excluded.password_hash, role=excluded.role, "
                "student_id=excluded.student_id",
                (username, password_hash(password), role, student_id),
            )
            db.execute("DELETE FROM auth_session WHERE username=?", (username,))

    def login(self, username: str, password: str, seconds: int) -> tuple[str, Principal] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT password_hash, role, student_id FROM account WHERE username=?", (username,)
            ).fetchone()
            if row is None or not verify_password(password, row[0]):
                return None
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM auth_session WHERE expires <= ?", (time.time(),))
            db.execute(
                "INSERT INTO auth_session VALUES (?, ?, ?)",
                (self.token_hash(token), username, time.time() + seconds),
            )
            return token, Principal(username, row[1], row[2])

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def principal(self, token: str | None) -> Principal | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT a.username, a.role, a.student_id FROM auth_session s "
                "JOIN account a ON a.username=s.username WHERE s.token_hash=? AND s.expires>?",
                (self.token_hash(token), time.time()),
            ).fetchone()
        return Principal(*row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM auth_session WHERE token_hash=?", (self.token_hash(token),))


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
    check_origin(request)
    principal = current_principal(request)
    request.state.principal = principal
    if principal.role == "teacher":
        return
    route = f"/api{request.scope['route'].path}"
    method = request.method
    shared = {
        ("GET", "/api/events"),
        ("GET", "/api/students"),
        ("GET", "/api/tasks"),
        ("GET", "/api/knowledge/points"),
        ("GET", "/api/knowledge/path"),
    }
    if (method, route) in shared:
        if route == "/api/tasks":
            requested = request.query_params.get("student_id")
            if requested and requested != principal.student_id:
                raise HTTPException(403, "无权访问其他学生的数据")
        return
    params = request.path_params
    student_routes = {
        ("GET", "/api/students/{student_id}/timeline"),
        ("GET", "/api/students/{student_id}/mastery"),
        ("GET", "/api/students/{student_id}/verification"),
        ("POST", "/api/students/{student_id}/question-photo"),
        ("POST", "/api/students/{student_id}/questions"),
    }
    if (method, route) in student_routes:
        if params["student_id"] != principal.student_id:
            raise HTTPException(403, "无权访问其他学生的数据")
        return
    task_routes = {
        ("GET", "/api/tasks/{task_id}"),
        ("GET", "/api/tasks/{task_id}/messages"),
        ("GET", "/api/tasks/{task_id}/answer-target"),
        ("GET", "/api/tasks/{task_id}/diagnosis"),
        ("POST", "/api/tasks/{task_id}/start"),
        ("POST", "/api/tasks/{task_id}/messages"),
        ("POST", "/api/tasks/{task_id}/answer-photo"),
    }
    if (method, route) in task_routes:
        with Session(request.app.state.orchestrator.engine) as session:
            task = session.get(Task, int(params["task_id"]))
            if task is None or task.student_id != principal.student_id:
                raise HTTPException(404, "任务不存在")
        return
    if "evidence_id" in params and method == "GET":
        with Session(request.app.state.orchestrator.engine) as session:
            evidence = session.get(Evidence, int(params["evidence_id"]))
            if evidence is None or evidence.student_id != principal.student_id:
                raise HTTPException(404, "证据不存在")
        return
    raise HTTPException(403, "需要教师权限")
