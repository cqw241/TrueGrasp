"""登录、当前会话与退出。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import COOKIE, check_origin, current_principal
from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


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
