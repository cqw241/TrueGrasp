"""Teacher-owned class rosters and account setup."""

from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth import business_error
from app.classroom import Classroom, Enrollment, StudentPlacement, active_class, write_lock
from app.contracts.entities import Student, Task
from app.contracts.enums import TaskStatus
from app.orchestration.storage import PendingWork, TeacherTaskControl

router = APIRouter(prefix="/classes", tags=["classes"])
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class ClassName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name


class ClassView(ClassName):
    class_id: str
    teacher_id: str


class StudentAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: Name | None = None
    name: Name | None = None
    student_number: Name | None = None

    @model_validator(mode="after")
    def choose_identity(self):
        if (self.student_id is None) == (self.name is None):
            raise ValueError("请选择新建学生或关联已有学生")
        return self


class MemberView(BaseModel):
    class_id: str
    student_id: str
    name: str
    student_number: str | None
    status: Literal["active", "left"]


class RosterImport(BaseModel):
    rows: list[list[str]] = Field(min_length=1, max_length=500)


class RosterResult(BaseModel):
    row: int
    status: Literal["added", "duplicate", "missing", "failed"]
    message: str
    student: MemberView | None = None


class SetupLink(BaseModel):
    username: str
    setup_path: str
    expires_in: int


def fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, business_error(code, message))


def session_for(request: Request) -> Session:
    return Session(request.app.state.orchestrator.engine)


def classroom(session: Session, request: Request, class_id: str) -> Classroom:
    row = session.get(Classroom, class_id)
    if row is None or row.teacher_id != request.state.principal.teacher_id:
        raise fail(404, "class_not_found", "班级不存在")
    return row


def class_view(row: Classroom) -> ClassView:
    return ClassView(class_id=row.id, name=row.name, teacher_id=row.teacher_id)


def member_view(row: Enrollment, student: Student) -> MemberView:
    return MemberView(
        class_id=row.class_id,
        student_id=row.student_id,
        name=student.name,
        student_number=row.student_number,
        status=row.status,
    )


@router.get("")
def list_classes(request: Request) -> list[ClassView]:
    with session_for(request) as session:
        return [
            class_view(row)
            for row in session.exec(
                select(Classroom)
                .where(Classroom.teacher_id == request.state.principal.teacher_id)
                .order_by(Classroom.name, Classroom.id)
            )
        ]


@router.post("", status_code=201)
def create_class(body: ClassName, request: Request) -> ClassView:
    row = Classroom(
        id="cls_" + uuid4().hex, name=body.name, teacher_id=request.state.principal.teacher_id
    )
    with session_for(request) as session:
        session.add(row)
        session.commit()
        return class_view(row)


@router.patch("/{class_id}")
def rename_class(class_id: str, body: ClassName, request: Request) -> ClassView:
    with session_for(request) as session:
        write_lock(session)
        row = classroom(session, request, class_id)
        row.name = body.name
        session.commit()
        return class_view(row)


@router.get("/{class_id}/students")
def list_members(class_id: str, request: Request, include_left: bool = False) -> list[MemberView]:
    with session_for(request) as session:
        classroom(session, request, class_id)
        query = select(Enrollment, Student).join(Student).where(Enrollment.class_id == class_id)
        if not include_left:
            query = query.where(Enrollment.status == "active")
        return [
            member_view(row, student)
            for row, student in session.exec(query.order_by(Student.name, Student.id))
        ]


@router.get("/available-students")
def available_students(request: Request) -> list[Student]:
    """Candidates are unassigned to an active class and explicitly managed by this teacher."""
    with session_for(request) as session:
        return list(
            session.exec(
                select(Student)
                .join(StudentPlacement)
                .where(
                    StudentPlacement.teacher_id == request.state.principal.teacher_id,
                    Student.id.not_in(
                        select(Enrollment.student_id).where(Enrollment.status == "active")
                    ),
                )
                .order_by(Student.name, Student.id)
            )
        )


def add_member(session: Session, row: Classroom, body: StudentAdd) -> MemberView:
    member = None
    if body.student_id:
        placement = session.get(StudentPlacement, body.student_id)
        if placement is None or placement.teacher_id != row.teacher_id:
            raise fail(404, "student_not_found", "没有可关联的学生")
        student = session.get(Student, body.student_id)
        member = session.get(Enrollment, (row.id, student.id))
        if member is not None and member.status == "active":
            raise fail(409, "duplicate_member", "学生已在本班")
        if active_class(session, student.id) is not None:
            raise fail(409, "active_class_conflict", "学生已在另一个班级")
    else:
        student = Student(id="stu_" + uuid4().hex, name=body.name)
        session.add(student)
        session.flush()
        session.add(StudentPlacement(student_id=student.id, teacher_id=row.teacher_id))
    number = (
        body.student_number
        if body.student_number is not None
        else (member.student_number if member else None)
    )
    if (
        number
        and session.exec(
            select(Enrollment).where(
                Enrollment.class_id == row.id,
                Enrollment.student_number == number,
                Enrollment.student_id != student.id,
            )
        ).first()
        is not None
    ):
        raise fail(409, "student_number_conflict", "该学号已被本班名单使用")
    if member is None:
        member = Enrollment(class_id=row.id, student_id=student.id)
    member.student_number = number
    member.status = "active"
    session.add(member)
    session.flush()
    return member_view(member, student)


@router.post("/{class_id}/students", status_code=201)
def enroll(class_id: str, body: StudentAdd, request: Request) -> MemberView:
    try:
        with session_for(request) as session:
            write_lock(session)
            result = add_member(session, classroom(session, request, class_id), body)
            session.commit()
            return result
    except IntegrityError as exc:
        raise fail(409, "enrollment_conflict", "名单发生冲突，请刷新后重试") from exc


@router.post("/{class_id}/students/import")
def import_roster(class_id: str, body: RosterImport, request: Request) -> list[RosterResult]:
    results = []
    with session_for(request) as session:
        write_lock(session)
        group = classroom(session, request, class_id)
        for index, fields in enumerate(body.rows, 1):
            if not fields or not fields[0].strip():
                results.append(RosterResult(row=index, status="missing", message="缺少姓名"))
                continue
            if len(fields) != 2 or not fields[1].strip():
                status = "missing" if len(fields) <= 2 else "failed"
                results.append(
                    RosterResult(row=index, status=status, message="每行需填写姓名和班内学号")
                )
                continue
            try:
                student = StudentAdd(name=fields[0], student_number=fields[1])
                with session.begin_nested():
                    member = add_member(session, group, student)
                results.append(
                    RosterResult(row=index, status="added", message="已添加", student=member)
                )
            except HTTPException as exc:
                results.append(
                    RosterResult(row=index, status="duplicate", message=exc.detail["message"])
                )
            except (ValueError, IntegrityError):
                results.append(
                    RosterResult(
                        row=index, status="failed", message="姓名或学号无效，或名单发生冲突"
                    )
                )
        session.commit()
    return results


@router.delete("/{class_id}/students/{student_id}", status_code=204)
def remove_member(class_id: str, student_id: str, request: Request) -> None:
    with session_for(request) as session:
        write_lock(session)
        classroom(session, request, class_id)
        row = session.get(Enrollment, (class_id, student_id))
        if row is None:
            raise fail(404, "student_not_found", "该班没有此学生")
        if row.status == "left":
            return
        row.status = "left"
        request.app.state.auth_store.revoke_setup(student_id)
        for task in session.exec(
            select(Task).where(
                Task.class_id == class_id,
                Task.student_id == student_id,
                Task.status.in_((TaskStatus.PENDING, TaskStatus.ACTIVE)),
            )
        ):
            task.status = TaskStatus.CANCELLED
            if work := session.get(PendingWork, f"task:{task.id}"):
                work.pending = False
            if control := session.get(TeacherTaskControl, task.id):
                control.taken_over = False
        session.commit()


@router.post("/{class_id}/students/{student_id}/setup-link", status_code=201)
def issue_setup(class_id: str, student_id: str, request: Request, response: Response) -> SetupLink:
    with session_for(request) as session:
        write_lock(session)
        classroom(session, request, class_id)
        row = session.get(Enrollment, (class_id, student_id))
        if row is None or row.status != "active":
            raise fail(404, "student_not_found", "该班没有此在班学生")
        try:
            username, token = request.app.state.auth_store.issue_setup(student_id, class_id)
        except ValueError as exc:
            raise fail(409, "account_conflict", str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return SetupLink(username=username, setup_path=f"/student-setup#{token}", expires_in=86400)
