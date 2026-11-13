"""教师注册、登录、当前会话与退出。"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlmodel import Session

from app.auth import COOKIE, business_error, check_origin, current_principal, password_hash
from app.classroom import active_class, write_lock
from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class TeacherRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    password: str = Field(min_length=12, max_length=128)


class User(BaseModel):
    username: str
    role: Literal["teacher", "student"]
    student_id: str | None


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> User:
    check_origin(request)
    settings = get_settings()
    seconds = settings.auth_session_hours * 3600
    result = request.app.state.auth_store.login(body.username, body.password, seconds)
    if result is None:
        raise HTTPException(401, "账号或密码错误")
    token, principal = result
    request.app.state.auth_store.logout(request.cookies.get(COOKIE))
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        max_age=seconds,
        path="/",
    )
    return User(username=principal.username, role=principal.role, student_id=principal.student_id)


@router.post("/register", status_code=201)
def register_teacher(body: TeacherRegistration, request: Request, response: Response) -> User:
    check_origin(request)
    if not request.app.state.auth_store.create_teacher(body.username, body.password):
        raise HTTPException(
            409, business_error("username_taken", "账号已存在，请更换账号或直接登录")
        )
    response.headers["Cache-Control"] = "no-store"
    return login(Login(username=body.username, password=body.password), request, response)


@router.get("/me")
def me(request: Request) -> User:
    principal = current_principal(request)
    return User(username=principal.username, role=principal.role, student_id=principal.student_id)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    check_origin(request)
    request.app.state.auth_store.logout(request.cookies.get(COOKIE))
    response.delete_cookie(
        COOKIE, path="/", secure=get_settings().auth_cookie_secure, httponly=True, samesite="strict"
    )


class StudentSetup(BaseModel):
    token: str = Field(min_length=40, max_length=128)
    password: str = Field(min_length=12, max_length=128)


class SetupComplete(BaseModel):
    username: str


@router.post("/student-setup")
def setup_student(body: StudentSetup, request: Request, response: Response) -> SetupComplete:
    try:
        check_origin(request)
    except HTTPException as exc:
        raise HTTPException(403, business_error("origin_forbidden", "请求来源不允许")) from exc
    store = request.app.state.auth_store
    invalid = HTTPException(404, business_error("invalid_setup", "链接无效或已失效，请联系教师"))
    identity = store.setup_identity(body.token)
    if identity is None:
        raise invalid
    student_id, class_id = identity
    hashed = password_hash(body.password)
    with Session(request.app.state.orchestrator.engine) as session:
        write_lock(session)
        if active_class(session, student_id) != class_id:
            raise invalid
        username = store.redeem_setup(body.token, hashed, student_id)
    if username is None:
        raise invalid
    response.headers["Cache-Control"] = "no-store"
    return SetupComplete(username=username)
