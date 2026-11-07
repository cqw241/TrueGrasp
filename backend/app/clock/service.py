"""确定性的教学日期、到期通知与章前预检，复用编排层的持久化待办。"""

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import Engine
from sqlmodel import Session, select

from app.clock.storage import DueNotification, LectureEntry, PrecheckContext, TeachingClockState
from app.contracts.api import ClockState
from app.contracts.entities import Hypothesis, Mastery, Misconception, Student, Task
from app.contracts.enums import HypothesisStatus, MasteryState, TaskStatus, TaskType
from app.contracts.events import (
    ClockAdvanced,
    EventPublisher,
    LectureEntered,
    TaskChanged,
    TaskDue,
    make_event,
)
from app.contracts.knowledge import KnowledgeGraph
from app.contracts.values import SLIP
from app.orchestration.storage import queue_work


class ClockError(ValueError):
    """调用边界上可直接向用户解释的日期操作错误。"""


def shanghai_today() -> date:
    # 当前中国教学日期使用 UTC+8；不依赖 Windows 上可能缺失的系统时区库。
    return datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).date()


class TeachingClock:
    def __init__(
        self,
        engine: Engine,
        knowledge: KnowledgeGraph,
        publisher: EventPublisher,
        *,
        mode: Literal["demo", "real"] = "demo",
        initial_date: date = date(2026, 9, 7),
        today: Callable[[], date] = shanghai_today,
        wake: Callable[[], None] = lambda: None,
    ) -> None:
        self.engine = engine
        self.knowledge = knowledge
        self.publisher = publisher
        self.mode = mode
        self.today = today
        self.wake = wake
        self._lock = asyncio.Lock()
        with Session(engine) as session:
            if session.get(TeachingClockState, 1) is None:
                session.add(
                    TeachingClockState(teaching_date=today() if mode == "real" else initial_date)
                )
                session.commit()

    def state(self) -> ClockState:
        with Session(self.engine) as session:
            row = session.get(TeachingClockState, 1)
            return ClockState(
                teaching_date=self.today() if self.mode == "real" else row.teaching_date,
                lecture=row.lecture,
            )

    def needs_date_sync(self) -> bool:
        with Session(self.engine) as session:
            return session.get(TeachingClockState, 1).teaching_date != self.today()

    def current_date(self) -> date:
        return self.state().teaching_date

    @staticmethod
    def _event(session: Session, payload, when: date, student_id: str | None = None) -> int:
        event = make_event(payload, when, student_id)
        session.add(event)
        session.flush()
        queue_work(session, f"event:{event.id}", {"dispatch": True})
        return event.id

    def _scan_due(self, session: Session, when: date) -> None:
        notified = select(DueNotification.task_id)
        tasks = session.exec(
            select(Task).where(
                Task.status == TaskStatus.PENDING,
                Task.due_date <= when,
                Task.id.not_in(notified),
            )
        ).all()
        for task in tasks:
            event_id = self._event(session, TaskDue(task_id=task.id), when, task.student_id)
            session.add(DueNotification(task_id=task.id, event_id=event_id))

    async def scan(self) -> ClockState:
        """启动或真实日期跨日调用；重复调用不会重复通知。"""
        return await self._change()

    async def advance(self, days: int = 1) -> ClockState:
        if self.mode == "real":
            raise ClockError("真实日期模式不支持手动推进教学日期")
        if days < 1:
            raise ClockError("推进天数必须大于 0")
        return await self._change(days=days)

    async def enter_lecture(self, lecture: int) -> ClockState:
        if not 1 <= lecture <= 12:
            raise ClockError("讲次必须在 1 到 12 之间")
        return await self._change(lecture=lecture)

    async def _change(self, *, days: int = 0, lecture: int | None = None) -> ClockState:
        async with self._lock:
            with Session(self.engine) as session:
                row = session.get(TeachingClockState, 1)
                previous = (row.teaching_date, row.lecture)
                try:
                    row.teaching_date = (
                        self.today()
                        if self.mode == "real"
                        else row.teaching_date + timedelta(days=days)
                    )
                except OverflowError as exc:
                    raise ClockError("推进后的日期超出支持范围") from exc
                created = []
                if lecture is not None:
                    row.lecture = lecture
                    created = self._prechecks(session, lecture, row.teaching_date)
                    if session.get(LectureEntry, lecture) is None:
                        event_id = self._event(
                            session, LectureEntered(lecture=lecture), row.teaching_date
                        )
                        session.add(LectureEntry(lecture=lecture, event_id=event_id))
                self._scan_due(session, row.teaching_date)
                state = ClockState(teaching_date=row.teaching_date, lecture=row.lecture)
                changed = previous != (row.teaching_date, row.lecture)
                session.commit()
                # 在提交之后通知；数据库中的事件和待办是恢复依据，SSE 仅用于刷新视图。
                self.wake()
                for task in created:
                    await self.publisher.publish(
                        TaskChanged(
                            task_id=task.id,
                            student_id=task.student_id,
                            task_type=task.type,
                            status=task.status,
                        )
                    )
                if changed:
                    await self.publisher.publish(ClockAdvanced(**state.model_dump()))
                return state

    def _prechecks(self, session: Session, lecture: int, when: date) -> list[Task]:
        targets = self.knowledge.lecture_points(lecture)
        students = session.exec(select(Student).order_by(Student.id)).all()
        mastery = {
            (m.student_id, m.knowledge_point_id): m for m in session.exec(select(Mastery)).all()
        }
        misconceptions = {m.id: m for m in session.exec(select(Misconception)).all()}
        confirmed: dict[str, list[tuple[Hypothesis, Misconception]]] = {}
        for hypothesis in session.exec(
            select(Hypothesis).where(Hypothesis.status == HypothesisStatus.SUPPORTED)
        ):
            mis = misconceptions.get(hypothesis.misconception_id)
            if mis is not None and mis.id != SLIP:
                confirmed.setdefault(hypothesis.student_id, []).append((hypothesis, mis))
        existing = {
            (p.student_id, p.knowledge_point_id, p.context_knowledge_point_id)
            for p in session.exec(select(PrecheckContext).where(PrecheckContext.lecture == lecture))
        }
        created = []
        for target in targets:
            prerequisite_ids = {p.id for p in self.knowledge.prerequisites(target.id)}
            for student in students:
                history = confirmed.get(student.id, [])
                # 复发关系可以指向非直接先修的旧知识点。
                old_ids = prerequisite_ids | {
                    mis.knowledge_point_id for _, mis in history if target.id in mis.recurs_in
                }
                for old_id in sorted(old_ids):
                    if (student.id, old_id, target.id) in existing:
                        continue
                    row = mastery.get((student.id, old_id))
                    relevant = [(h, m) for h, m in history if m.knowledge_point_id == old_id]
                    reasons = []
                    if old_id in prerequisite_ids:
                        if relevant:
                            reasons.append("直接先修知识点有已确认错因")
                        if row is None or row.state not in (
                            MasteryState.CONSOLIDATED,
                            MasteryState.INTEGRATED,
                        ):
                            reasons.append("直接先修知识点未达到巩固（含缺失掌握记录）")
                    if any(target.id in m.recurs_in for _, m in relevant):
                        reasons.append("已确认错因可能在新知识点复发")
                    if not reasons:
                        continue
                    evidence_ids = set(row.evidence_ids if row else [])
                    for hypothesis, _ in relevant:
                        evidence_ids.update(hypothesis.evidence_ids)
                        evidence_ids.add(hypothesis.error_evidence_id)
                    task = Task(
                        type=TaskType.PRECHECK,
                        student_id=student.id,
                        knowledge_point_id=old_id,
                        misconception_ids=sorted({m.id for _, m in relevant}),
                        evidence_ids=sorted(evidence_ids),
                        reason=f"进入第 {lecture} 讲，预检 {target.id}：" + "；".join(reasons),
                        due_date=when,
                        created_on=when,
                    )
                    session.add(task)
                    session.flush()
                    session.add(
                        PrecheckContext(
                            lecture=lecture,
                            student_id=student.id,
                            knowledge_point_id=old_id,
                            context_knowledge_point_id=target.id,
                            task_id=task.id,
                        )
                    )
                    existing.add((student.id, old_id, target.id))
                    created.append(task)
        return created
