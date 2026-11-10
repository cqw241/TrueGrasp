"""Teacher decisions, append-only evidence and coordination with the task runtime."""

from datetime import date

from fastapi import HTTPException
from sqlmodel import Session, select

from app.clock.storage import DueNotification
from app.contracts.api import AdoptMisconception, Escalation, GoalCreate, TaskControl, TaskOverride
from app.contracts.entities import (
    Evidence,
    KnowledgePoint,
    Misconception,
    Student,
    Task,
    TeachingGoal,
)
from app.contracts.enums import ContentSource, EvidenceKind, JudgedBy, TaskStatus, TaskType
from app.contracts.events import EvidenceAdded, TeacherInstruction, make_event
from app.knowledge.graph import NxKnowledgeGraph
from app.ledger.queries import RANK
from app.ledger.service import append_evidence
from app.orchestration.runtime import INTERACTIVE_TYPES, Orchestrator
from app.orchestration.storage import (
    ClassTaskOverride,
    PendingWork,
    TeacherTaskControl,
    queue_work,
    teacher_has_student,
)

OPEN = (TaskStatus.PENDING, TaskStatus.ACTIVE)


def _date(runtime: Orchestrator) -> date:
    return runtime.teaching_date() if runtime.teaching_date else date.today()


def _queue_instruction(session, payload, today, student_id=None) -> None:
    event = make_event(payload, today, student_id)
    session.add(event)
    session.flush()
    queue_work(session, f"event:{event.id}", {"dispatch": True})


def set_goal(runtime: Orchestrator, body: GoalCreate) -> TeachingGoal:
    point_ids = list(dict.fromkeys(body.knowledge_point_ids))
    if not point_ids or body.target_state not in RANK:
        raise HTTPException(422, "请选择知识点及初步掌握、巩固或融会贯通目标")
    today = _date(runtime)
    with Session(runtime.engine, expire_on_commit=False) as session:
        existing = set(
            session.exec(select(KnowledgePoint.id).where(KnowledgePoint.id.in_(point_ids)))
        )
        if missing := set(point_ids) - existing:
            raise HTTPException(422, "未知知识点：" + "、".join(sorted(missing)))
        goal = TeachingGoal(
            **body.model_dump(exclude={"knowledge_point_ids"}),
            knowledge_point_ids=point_ids,
            created_on=today,
        )
        session.add(goal)
        session.flush()
        _queue_instruction(session, TeacherInstruction(action="set_goal", ref_id=goal.id), today)
        session.commit()
    runtime.recover_pending()
    return goal


def list_escalations(session: Session) -> list[Escalation]:
    rows = session.exec(
        select(Task, Student.name)
        .join(Student, Student.id == Task.student_id)
        .where(Task.type == TaskType.ESCALATION, Task.status != TaskStatus.CANCELLED)
        .order_by(Task.status == TaskStatus.DONE, Task.id.desc())
    ).all()
    ids = {eid for task, _ in rows for eid in task.evidence_ids}
    evidence = {e.id: e for e in session.exec(select(Evidence).where(Evidence.id.in_(ids)))}
    return [
        Escalation(
            task=task,
            student_name=name,
            evidence=sorted(
                (evidence[eid] for eid in set(task.evidence_ids) if eid in evidence),
                key=lambda e: (e.teaching_date, e.id),
            ),
            suggestion=task.reason,
        )
        for task, name in rows
    ]


def _record(
    session: Session, task: Task, today: date, reason: str, misconception_id: str | None = None
) -> Evidence:
    return append_evidence(
        session,
        Evidence(
            student_id=task.student_id,
            knowledge_point_id=task.knowledge_point_id,
            kind=EvidenceKind.TEACHER,
            task_id=task.id,
            judged_by=JudgedBy.TEACHER,
            reason=reason,
            teaching_date=today,
            matched_misconception_id=misconception_id,
        ),
    )


async def _publish_evidence(runtime, evidence: Evidence | None) -> None:
    if evidence is not None:
        await runtime.bus.publish(
            EvidenceAdded(
                student_id=evidence.student_id,
                evidence_id=evidence.id,
                knowledge_point_id=evidence.knowledge_point_id,
            )
        )


def _assert_open(task: Task) -> None:
    if task.status not in OPEN:
        raise HTTPException(409, "任务已结束")


async def resolve_escalation(runtime: Orchestrator, task_id: int, note: str) -> Task:
    note = note.strip()
    if not note:
        raise HTTPException(422, "请填写处理说明")
    async with runtime._lock(f"task:{task_id}"):
        with Session(runtime.engine, expire_on_commit=False) as session:
            task = runtime._task(session, task_id)
            if task.type != TaskType.ESCALATION:
                raise HTTPException(409, "该任务不是教师转交")
            _assert_open(task)
            today = _date(runtime)
            evidence = _record(session, task, today, f"教师处理转交：{note}")
            task.evidence_ids = [*task.evidence_ids, evidence.id]
            task.status = TaskStatus.DONE
            _queue_instruction(
                session,
                TeacherInstruction(action="resolve_escalation", ref_id=task.id, note=note),
                today,
                task.student_id,
            )
            session.commit()
        await _publish_evidence(runtime, evidence)
        await runtime._publish_task(task)
    runtime.recover_pending()
    return task


async def override_task(runtime: Orchestrator, task_id: int, body: TaskOverride) -> Task:
    note = body.note.strip()
    key = f"task:{task_id}"
    async with runtime._lock(key):
        with Session(runtime.engine, expire_on_commit=False) as session:
            task = runtime._task(session, task_id)
            _assert_open(task)
            today = _date(runtime)
            control = session.get(TeacherTaskControl, task_id) or TeacherTaskControl(
                task_id=task_id
            )
            control.overridden = True
            control.note = note
            old_due = task.due_date
            released = body.cancel and control.taken_over
            if body.cancel:
                task.status = TaskStatus.CANCELLED
                control.taken_over = False
            elif body.due_date is not None:
                task.due_date = body.due_date
            rescheduled = old_due != task.due_date and task.status == TaskStatus.PENDING
            if rescheduled and (notification := session.get(DueNotification, task_id)):
                session.delete(notification)
            if (body.cancel or rescheduled) and (work := session.get(PendingWork, key)):
                work.pending = False
            session.add(control)
            action = (
                "取消计划" if body.cancel else f"覆盖计划（到期日 {old_due} → {task.due_date}）"
            )
            if task.student_id is None:
                session.add(
                    ClassTaskOverride(
                        task_id=task_id,
                        note=note,
                        action=action,
                        teaching_date=today,
                    )
                )
                evidence = None
            else:
                evidence = _record(
                    session, task, today, f"教师{action}：{note}" if note else f"教师{action}"
                )
            session.commit()
        await _publish_evidence(runtime, evidence)
        await runtime._publish_task(task)
    if released:
        runtime.recover_pending()
    return task


async def set_takeover(
    runtime: Orchestrator, task_id: int, *, taken_over: bool, note: str = ""
) -> TaskControl:
    key = f"task:{task_id}"
    async with runtime._lock(key):
        with Session(runtime.engine, expire_on_commit=False) as session:
            task = runtime._task(session, task_id)
            _assert_open(task)
            if task.type not in (TaskType.TUTORING, TaskType.QUESTION):
                raise HTTPException(409, "只有辅导会话可以接管")
            control = session.get(TeacherTaskControl, task_id) or TeacherTaskControl(
                task_id=task_id
            )
            if control.taken_over == taken_over:
                raise HTTPException(409, "辅导已由教师接管" if taken_over else "辅导未被接管")
            student_tasks = list(
                session.exec(
                    select(Task).where(
                        Task.student_id == task.student_id,
                        Task.status.in_(OPEN),
                        Task.type.in_(INTERACTIVE_TYPES),
                    )
                )
            )
            if taken_over:
                if teacher_has_student(session, task.student_id):
                    raise HTTPException(409, "该学生已有教师接管的会话")
                for active in student_tasks:
                    active_key = f"task:{active.id}"
                    if active_key in runtime._jobs or (
                        active.id != task.id and runtime._lock(active_key).locked()
                    ):
                        raise HTTPException(409, "正在处理上一轮作答，请稍候再接管")
            today = _date(runtime)
            control.taken_over = taken_over
            if note.strip():
                control.note = note.strip()
            session.add(control)
            action = "教师接管辅导" if taken_over else "教师交还 Agent"
            evidence = _record(
                session, task, today, f"{action}：{note.strip()}" if note.strip() else action
            )
            if not taken_over and task.status == TaskStatus.ACTIVE:
                queue_work(session, key, {"teaching_date": today.isoformat()}, reactivate=True)
            session.commit()
        await _publish_evidence(runtime, evidence)
        for affected in student_tasks:
            await runtime._publish_task(affected)
    if not taken_over:
        runtime._failed_work.discard(key)
        runtime.recover_pending()
    return TaskControl.model_validate(control, from_attributes=True)


async def adopt_misconception(
    runtime: Orchestrator,
    knowledge: NxKnowledgeGraph,
    misconception_id: str,
    body: AdoptMisconception,
) -> Misconception:
    with Session(runtime.engine, expire_on_commit=False) as session:
        misconception = session.get(Misconception, misconception_id)
        if misconception is None:
            raise HTTPException(404, "错因不存在")
        if misconception.source != ContentSource.UNMODELED:
            raise HTTPException(409, "该错因不在待审核列表")
        if body.description is not None:
            description = body.description.strip()
            if not description:
                raise HTTPException(422, "错因描述不能为空")
            misconception.description = description
        misconception.source = ContentSource.TEACHER
        origin = session.exec(
            select(Evidence)
            .where(
                Evidence.matched_misconception_id == misconception_id,
                Evidence.kind != EvidenceKind.TEACHER,
            )
            .order_by(Evidence.id)
        ).first()
        evidence = None
        if origin is not None and origin.task_id is not None:
            task = runtime._task(session, origin.task_id)
            evidence = _record(
                session,
                task,
                _date(runtime),
                f"教师采纳错因 {misconception.id}：{misconception.description}",
                misconception.id,
            )
        session.commit()
        knowledge.add_adopted_misconception(misconception)
        if runtime.knowledge is not knowledge and isinstance(runtime.knowledge, NxKnowledgeGraph):
            runtime.knowledge.add_adopted_misconception(misconception)
    await _publish_evidence(runtime, evidence)
    return misconception
