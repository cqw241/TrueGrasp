"""可注入 Agent 的异步编排运行时。业务状态在 SQLModel，交互状态在 checkpoint。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.contracts.agents import (
    Judgement,
    Planner,
    PlannerInput,
    PlannerOutput,
    Tutor,
    TutorTask,
    TutorTurn,
    TutorTurnInput,
    Verifier,
)
from app.contracts.api import ChatTurn, MessageCreate
from app.contracts.entities import Event, Evidence, Item, Task
from app.contracts.enums import (
    Dimension,
    EventType,
    EvidenceKind,
    ItemPurpose,
    TaskStatus,
    TaskType,
    TutorOutcome,
)
from app.contracts.events import (
    AnswerSubmitted,
    EvidenceAdded,
    MessageDone,
    TaskChanged,
    TaskDue,
    make_event,
    read_payload,
)
from app.contracts.knowledge import KnowledgeGraph
from app.contracts.llm import ChatMessage
from app.contracts.values import SLIP
from app.orchestration.bus import EventBus
from app.orchestration.graphs import RunState, build_graph
from app.orchestration.inputs import build_verifier_input
from app.orchestration.storage import (
    NodeReceipt,
    PendingWork,
    init_storage,
    queue_work,
    record_exposure,
)

logger = logging.getLogger(__name__)
INTERACTIVE_TYPES = {TaskType.TUTORING, TaskType.VERIFICATION, TaskType.RETEST, TaskType.PRECHECK}


class OrchestrationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


def append_evidence(session: Session, evidence: Evidence) -> Evidence:
    """T19 的替换点：使用调用方事务追加证据，不能自行 commit。"""
    session.add(evidence)
    session.flush()
    return evidence


class Orchestrator:
    def __init__(
        self,
        engine: Engine,
        checkpointer: AsyncSqliteSaver,
        bus: EventBus,
        *,
        planner: Planner | None = None,
        tutor: Tutor | None = None,
        verifier: Verifier | None = None,
        knowledge: KnowledgeGraph | None = None,
        evidence_writer: Callable[[Session, Evidence], Evidence] = append_evidence,
        teaching_date: Callable[[], date] | None = None,
    ) -> None:
        self.engine = engine
        self.bus = bus
        self.planner = planner
        self.tutor = tutor
        self.verifier = verifier
        self.knowledge = knowledge
        self.evidence_writer = evidence_writer
        self.teaching_date = teaching_date
        self.graph = build_graph(self, checkpointer)
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._errors: list[Exception] = []
        self._closed = False

    @classmethod
    @asynccontextmanager
    async def open(
        cls, engine: Engine, checkpoint_path: str, bus: EventBus, **dependencies: Any
    ) -> AsyncIterator[Orchestrator]:
        init_storage(engine)
        if checkpoint_path != ":memory:":
            Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as saver:
            await saver.setup()
            runtime = cls(engine, saver, bus, **dependencies)
            try:
                runtime.recover_pending()
                yield runtime
            finally:
                await runtime.close()

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _require(self, *names: str) -> None:
        missing = [name for name in names if getattr(self, name) is None]
        if missing:
            raise OrchestrationError(501, f"尚未接入：{', '.join(missing)}")

    def _task(self, session: Session, task_id: int) -> Task:
        task = session.get(Task, task_id)
        if task is None:
            raise OrchestrationError(404, f"任务不存在：{task_id}")
        return task

    def _check_task(self, task_id: int, *, recovering: bool = False) -> Task:
        with Session(self.engine) as session:
            task = self._task(session, task_id)
            if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED) and not recovering:
                raise OrchestrationError(409, f"任务已结束：{task.status}")
            if task.type not in INTERACTIVE_TYPES or task.student_id is None:
                raise OrchestrationError(409, "该任务不接受学生交互")
            self._require("planner")
            if task.type == TaskType.TUTORING:
                self._require("tutor", "verifier", "knowledge")
            else:
                self._require("verifier")
            return task

    @staticmethod
    def _config(key: str) -> dict:
        return {"configurable": {"thread_id": key}}

    def _pending(self, key: str) -> bool:
        with Session(self.engine) as session:
            work = session.get(PendingWork, key)
            return work is not None and work.pending

    def _acknowledge(self, key: str) -> None:
        with Session(self.engine) as session:
            if work := session.get(PendingWork, key):
                work.pending = False
                session.commit()

    @staticmethod
    def _queue_task(session: Session, state: RunState) -> None:
        queue_work(
            session,
            f"task:{state['task_id']}",
            {"teaching_date": state["teaching_date"]},
            reactivate=True,
        )

    def _recovering_terminal(self, task_id: int, snapshot) -> bool:
        if snapshot.values.get("task_id") != task_id or snapshot.interrupts:
            return False
        with Session(self.engine) as session:
            task = self._task(session, task_id)
            node = {TaskStatus.DONE: "finish", TaskStatus.CANCELLED: "replan"}.get(task.status)
            if node is None or session.get(NodeReceipt, f"task:{task_id}:{node}") is None:
                return False
            work = session.get(PendingWork, f"task:{task_id}")
            return (
                work is not None
                and work.pending
                and (not snapshot.next or snapshot.next == (node,))
            )

    def recover_pending(self) -> None:
        """只恢复事务中已登记的工作；缺少真实 Agent 时保留待办，不生成模拟结果。"""
        if self.planner is None:
            return
        with Session(self.engine) as session:
            work_items = session.exec(select(PendingWork).where(PendingWork.pending)).all()
            for work in work_items:
                kind, _, target = work.key.partition(":")
                if kind == "task":
                    task = self._task(session, int(target))
                    if self.verifier is None or (
                        task.type == TaskType.TUTORING
                        and (self.tutor is None or self.knowledge is None)
                    ):
                        continue
                self._spawn(
                    work.key,
                    lambda key=work.key, payload=work.payload: self._run_work(key, payload),
                )

    async def _run_work(self, key: str, payload: dict[str, Any]) -> None:
        if not self._pending(key):
            return
        kind, _, target = key.partition(":")
        try:
            if kind == "event":
                await self.handle_event(int(target), dispatch=payload["dispatch"])
            else:
                await self.start_task(
                    int(target), teaching_date=date.fromisoformat(payload["teaching_date"])
                )
        except OrchestrationError:
            # 直接调用和后台恢复可能先后处理同一工作；已经确认的工作不再重开任务。
            if self._pending(key):
                raise

    def _spawn(self, key: str, action: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        if self._closed or key in self._jobs:
            return

        async def run() -> None:
            try:
                await action()
            except Exception as exc:
                self._errors.append(exc)
                logger.exception("编排后台执行失败：%s", key)
            finally:
                self._jobs.pop(key, None)

        self._jobs[key] = asyncio.create_task(run(), name=key)

    async def wait_idle(self) -> None:
        """验收和脚本使用：等当前作业执行到等待输入或结束，并抛出后台错误。"""
        while self._jobs:
            await asyncio.gather(*list(self._jobs.values()))
        if self._errors:
            errors, self._errors = self._errors, []
            raise ExceptionGroup("编排后台执行失败", errors)

    async def close(self) -> None:
        self._closed = True
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def handle_event(self, event_id: int, *, dispatch: bool = True) -> None:
        self._require("planner")
        key = f"event:{event_id}"
        async with self._lock(key):
            with Session(self.engine) as session:
                event = session.get(Event, event_id)
                if event is None:
                    raise OrchestrationError(404, f"事件不存在：{event_id}")
                read_payload(event)
                processed = event.processed
            snapshot = await self.graph.aget_state(self._config(key))
            if not processed or snapshot.next:
                await self.graph.ainvoke(
                    None
                    if snapshot.next
                    else RunState(kind="event", event_id=event_id, dispatch=dispatch),
                    self._config(key),
                    durability="sync",
                )
            self._acknowledge(key)
            self.recover_pending()

    async def start_task(
        self,
        task_id: int,
        *,
        teaching_date: date | None = None,
        dimensions: list[Dimension] | None = None,
        context_knowledge_point_id: str | None = None,
    ) -> None:
        key = f"task:{task_id}"
        async with self._lock(key):
            snapshot = await self.graph.aget_state(self._config(key))
            recovering = self._recovering_terminal(task_id, snapshot)
            task = self._check_task(task_id, recovering=recovering)
            if recovering:
                await self._run_task(key, None)
                return
            if snapshot.interrupts:
                self._acknowledge(key)
                return
            if teaching_date is None:
                teaching_date = (
                    self.teaching_date()
                    if self.teaching_date
                    else date.fromisoformat(snapshot.values["teaching_date"])
                    if snapshot.values
                    else task.created_on
                )
            if task.due_date is not None and task.due_date > teaching_date:
                raise OrchestrationError(409, "任务尚未到期")
            initial = RunState(
                kind="task",
                task_id=task_id,
                teaching_date=teaching_date.isoformat(),
                messages=[],
                shown_item_ids=[],
                incoming_message=None,
                dimensions=[dimension.value for dimension in dimensions]
                if dimensions is not None
                else None,
                context_knowledge_point_id=context_knowledge_point_id,
                followup_event_ids=[],
            )
            await self._run_task(key, None if snapshot.next else initial)

    async def _validate_message(self, task_id: int, body: MessageCreate):
        self._check_task(task_id)
        snapshot = await self.graph.aget_state(self._config(f"task:{task_id}"))
        if not snapshot.interrupts:
            raise OrchestrationError(409, "任务当前没有等待学生回复")
        if body.answering_item_id != snapshot.values.get("awaiting_item_id"):
            raise OrchestrationError(409, "回复的题目与当前待答题目不一致")
        return snapshot

    async def submit_message(
        self, task_id: int, body: MessageCreate, *, teaching_date: date | None = None
    ) -> None:
        key = f"task:{task_id}"
        async with self._lock(key):
            await self._validate_message(task_id, body)
            current_date = teaching_date or (self.teaching_date() if self.teaching_date else None)
            await self._run_task(
                key,
                Command(
                    resume=body.model_dump(mode="json"),
                    update={"teaching_date": current_date.isoformat()} if current_date else None,
                ),
            )

    async def enqueue_start(self, task_id: int) -> None:
        key = f"task:{task_id}"
        if key in self._jobs:
            snapshot = await self.graph.aget_state(self._config(key))
            self._check_task(task_id, recovering=self._recovering_terminal(task_id, snapshot))
            return
        async with self._lock(key):
            snapshot = await self.graph.aget_state(self._config(key))
            task = self._check_task(
                task_id, recovering=self._recovering_terminal(task_id, snapshot)
            )
            if snapshot.interrupts:
                return
            today = (
                self.teaching_date()
                if self.teaching_date
                else date.fromisoformat(snapshot.values["teaching_date"])
                if snapshot.values
                else task.created_on
            )
            if task.due_date is not None and task.due_date > today:
                raise OrchestrationError(409, "任务尚未到期")
            self._spawn(key, lambda: self.start_task(task_id))

    async def enqueue_message(self, task_id: int, body: MessageCreate) -> None:
        key = f"task:{task_id}"
        # 受理前校验并占用作业键，避免两次请求都消费同一个等待点。
        if key in self._jobs:
            raise OrchestrationError(409, "该任务正在处理上一条消息")
        async with self._lock(key):
            if key in self._jobs:
                raise OrchestrationError(409, "该任务正在处理上一条消息")
            await self._validate_message(task_id, body)
            self._spawn(key, lambda: self.submit_message(task_id, body))

    async def get_messages(self, task_id: int) -> list[ChatTurn]:
        with Session(self.engine) as session:
            self._task(session, task_id)
        snapshot = await self.graph.aget_state(self._config(f"task:{task_id}"))
        # SQLModel 的 table 模型作为嵌套字段时走构造路径；显式验证以还原枚举和值对象。
        return [
            ChatTurn(
                role=message["role"],
                content=message["content"],
                item=Item.model_validate(message["item"]) if message.get("item") else None,
            )
            for message in snapshot.values.get("messages", [])
        ]

    async def _run_task(self, key: str, inp: RunState | Command | None) -> None:
        before = await self.graph.aget_state(self._config(key))
        result = await self.graph.ainvoke(inp, self._config(key), durability="sync")
        new_messages = result.get("messages", [])[len(before.values.get("messages", [])) :]
        # graph 已经保存 checkpoint，前端现在可以读取完整消息。
        for message in new_messages:
            if message["role"] == "assistant":
                await self.bus.publish(MessageDone(task_id=result["task_id"]))
        for event_id in result.get("followup_event_ids", []):
            dispatch = event_id != result.get("replan_event_id")
            with Session(self.engine) as session:
                queue_work(session, f"event:{event_id}", {"dispatch": dispatch})
                session.commit()
        self._acknowledge(key)
        self.recover_pending()

    async def plan(self, state: RunState) -> dict:
        with Session(self.engine) as session:
            event = session.get(Event, state["event_id"])
            if event.processed:
                return {"plan": PlannerOutput().model_dump(mode="json")}
            inp = PlannerInput(event_id=event.id, teaching_date=event.teaching_date)
        result = await self.planner.plan(inp)
        return {"plan": result.model_dump(mode="json")}

    async def save_plan(self, state: RunState) -> dict:
        # 取消与该任务正在执行的一轮串行，避免完成节点覆盖已取消状态。
        async with AsyncExitStack() as stack:
            for task_id in sorted(set(state["plan"]["cancel_task_ids"])):
                await stack.enter_async_context(self._lock(f"task:{task_id}"))
            return await self._save_plan(state)

    async def _save_plan(self, state: RunState) -> dict:
        plan = PlannerOutput.model_validate(state["plan"])
        changed = []
        with Session(self.engine) as session:
            receipt_key = f"event:{state['event_id']}:save_plan"
            if receipt := session.get(NodeReceipt, receipt_key):
                return receipt.result
            event = session.get(Event, state["event_id"])
            if event.processed:
                return {"task_ids": state.get("task_ids", [])}
            for task_id in plan.cancel_task_ids:
                task = self._task(session, task_id)
                if task.status not in (TaskStatus.DONE, TaskStatus.CANCELLED):
                    task.status = TaskStatus.CANCELLED
                    changed.append(task)
                    if work := session.get(PendingWork, f"task:{task_id}"):
                        work.pending = False
            created = []
            for planned in plan.tasks:
                if (planned.type == TaskType.CLASS_REVIEW) != (planned.student_id is None):
                    raise ValueError("只有课上讲解建议的 student_id 可以为空")
                if planned.round < 1:
                    raise ValueError("任务轮次必须大于 0")
                task = Task(**planned.model_dump(), created_on=event.teaching_date)
                session.add(task)
                created.append(task)
            event.processed = True
            session.flush()
            task_ids = [task.id for task in created]
            payload = read_payload(event)
            if isinstance(payload, TaskDue) and payload.task_id not in task_ids:
                self._task(session, payload.task_id)
                task_ids.append(payload.task_id)
            changed.extend(created)
            dispatch = state.get("dispatch", True)
            result = {"task_ids": task_ids}
            session.add(NodeReceipt(key=receipt_key, result=result))
            queue_work(session, f"event:{event.id}", {"dispatch": dispatch})
            if dispatch:
                for task_id in task_ids:
                    task = self._task(session, task_id)
                    if (
                        task.status in (TaskStatus.PENDING, TaskStatus.ACTIVE)
                        and task.type in INTERACTIVE_TYPES
                        and (task.due_date is None or task.due_date <= event.teaching_date)
                    ):
                        queue_work(
                            session,
                            f"task:{task_id}",
                            {"teaching_date": event.teaching_date.isoformat()},
                            reactivate=True,
                        )
            session.commit()
            for task in changed:
                await self._publish_task(task)
        return result

    async def _publish_task(self, task: Task) -> None:
        await self.bus.publish(
            TaskChanged(
                task_id=task.id,
                student_id=task.student_id,
                task_type=task.type,
                status=task.status,
            )
        )

    async def activate(self, state: RunState) -> dict:
        with Session(self.engine) as session:
            task = self._task(session, state["task_id"])
            task.status = TaskStatus.ACTIVE
            self._queue_task(session, state)
            session.commit()
            await self._publish_task(task)
            return {"phase": "tutoring" if task.type == TaskType.TUTORING else "verifying"}

    def tutor_input(self, state: RunState) -> dict:
        with Session(self.engine) as session:
            task = self._task(session, state["task_id"])
            error = session.exec(
                select(Evidence)
                .where(Evidence.id.in_(task.evidence_ids), Evidence.is_correct == False)  # noqa: E712
                .order_by(Evidence.id.desc())
            ).first()
            tutor_task = TutorTask(
                task_id=task.id,
                student_id=task.student_id,
                knowledge_point_id=task.knowledge_point_id,
                error_evidence_id=error.id if error else None,
                allowed_backtrack_ids=[
                    point.id for point in self.knowledge.ancestors(task.knowledge_point_id)
                ],
                round=task.round,
            )
        incoming = state.get("incoming_message")
        history = state["messages"][:-1] if incoming else state["messages"]
        return TutorTurnInput(
            task=tutor_task,
            history=[ChatMessage(role=m["role"], content=m["content"]) for m in history],
            student_message=incoming["content"] if incoming else None,
            answering_item_id=incoming["answering_item_id"] if incoming else None,
        ).model_dump(mode="json")

    def save_tutor(self, state: RunState) -> dict:
        turn = TutorTurn.model_validate(state["tutor_turn"])
        item = None
        shown = state["shown_item_ids"]
        with Session(self.engine) as session:
            task = self._task(session, state["task_id"])
            if turn.awaiting_item_id is not None:
                item = session.get(Item, turn.awaiting_item_id)
                if item is None:
                    raise ValueError(f"辅导 Agent 返回的题目不存在：{turn.awaiting_item_id}")
                record_exposure(session, task.student_id, task.id, item)
                shown = list(dict.fromkeys([*shown, item.id]))
            if turn.outcome == TutorOutcome.REQUEST_VERIFICATION:
                if not turn.confirmed_misconception_ids:
                    raise ValueError("申请验证时必须给出已确认错因，偶发失误使用 slip")
                task.misconception_ids = turn.confirmed_misconception_ids
            self._queue_task(session, state)
            session.commit()
            if item is not None:
                session.refresh(item)
        return {
            "messages": [
                *state["messages"],
                ChatTurn(role="assistant", content=turn.message, item=item).model_dump(mode="json"),
            ],
            "shown_item_ids": shown,
            "awaiting_item_id": turn.awaiting_item_id,
            "incoming_message": None,
        }

    def prepare_verification(self, state: RunState) -> dict:
        with Session(self.engine) as session:
            task = self._task(session, state["task_id"])
            dimensions = state.get("dimensions")
            if dimensions is None:
                if task.type == TaskType.RETEST:
                    dimensions = [Dimension.RETENTION]
                elif task.misconception_ids == [SLIP]:
                    dimensions = [Dimension.CORRECTNESS]
                else:
                    dimensions = [
                        Dimension.CORRECTNESS,
                        Dimension.DISTRACTOR,
                        Dimension.NEAR_TRANSFER,
                    ]
            inp = build_verifier_input(
                session,
                student_id=task.student_id,
                knowledge_point_id=task.knowledge_point_id,
                dimensions=[Dimension(dimension) for dimension in dimensions],
                confirmed_misconception_ids=task.misconception_ids,
                shown_item_ids=state["shown_item_ids"],
                context_knowledge_point_id=state.get("context_knowledge_point_id"),
            )
        return {"phase": "verifying", "verifier_input": inp.model_dump(mode="json")}

    def save_items(self, state: RunState) -> dict:
        items = [Item.model_validate(item) for item in state["verification_items"]]
        if [item.dimension for item in items] != state["verifier_input"]["dimensions"]:
            raise ValueError("验证 Agent 必须按请求的维度逐一返回题目")
        if not items:
            raise ValueError("验证题不能为空")
        fingerprints = set(state["verifier_input"]["used_fingerprints"])
        with Session(self.engine) as session:
            for item in items:
                if item.purpose != ItemPurpose.VERIFICATION:
                    raise ValueError("验证 Agent 返回了非验证题")
                if item.fingerprint is None or item.fingerprint in fingerprints:
                    raise ValueError("验证题缺少指纹或与已用题目重复")
                fingerprints.add(item.fingerprint)
                stored = session.get(Item, item.id)
                if stored is None:
                    session.add(item)
                elif stored.model_dump() != item.model_dump():
                    raise ValueError(f"题目 ID 已存在且内容不同：{item.id}")
            session.commit()
        return {}

    def present_item(self, state: RunState) -> dict:
        item = Item.model_validate(state["verification_items"][state["item_index"]])
        with Session(self.engine) as session:
            task = self._task(session, state["task_id"])
            record_exposure(session, task.student_id, task.id, item)
            self._queue_task(session, state)
            session.commit()
        return {
            "messages": [
                *state["messages"],
                ChatTurn(role="assistant", content=item.stem, item=item).model_dump(mode="json"),
            ],
            "shown_item_ids": list(dict.fromkeys([*state["shown_item_ids"], item.id])),
            "awaiting_item_id": item.id,
            "incoming_message": None,
        }

    def accept_input(self, state: RunState) -> dict:
        message = MessageCreate.model_validate(state["incoming_message"])
        return {
            "messages": [
                *state["messages"],
                ChatTurn(role="user", content=message.content).model_dump(mode="json"),
            ]
        }

    async def save_evidence(self, state: RunState) -> dict:
        item = Item.model_validate(state["verification_items"][state["item_index"]])
        judgement = Judgement.model_validate(state["judgement"])
        with Session(self.engine) as session:
            task = self._task(session, state["task_id"])
            # 同一任务的一道验证题只接受一次作答，也让失败节点重试不重复追加。
            evidence = session.exec(
                select(Evidence).where(
                    Evidence.task_id == task.id,
                    Evidence.item_id == item.id,
                    Evidence.kind == EvidenceKind.VERIFICATION,
                )
            ).first()
            if evidence is None:
                evidence = self.evidence_writer(
                    session,
                    Evidence(
                        student_id=task.student_id,
                        knowledge_point_id=task.knowledge_point_id,
                        kind=EvidenceKind.VERIFICATION,
                        dimension=item.dimension,
                        task_id=task.id,
                        item_id=item.id,
                        student_answer=state["incoming_message"]["content"],
                        correct_answer=item.answer,
                        predictions=item.predictions,
                        **judgement.model_dump(),
                        teaching_date=date.fromisoformat(state["teaching_date"]),
                    ),
                )
            event = session.exec(
                select(Event).where(
                    Event.payload["evidence_id"].as_integer() == evidence.id,
                    Event.type == EventType.ANSWER_SUBMITTED,
                )
            ).first()
            if event is None:
                event = make_event(
                    AnswerSubmitted(task_id=task.id, evidence_id=evidence.id),
                    evidence.teaching_date,
                    task.student_id,
                )
                session.add(event)
            session.flush()
            queue_work(session, f"event:{event.id}", {"dispatch": True})
            self._queue_task(session, state)
            session.commit()
            await self.bus.publish(
                EvidenceAdded(
                    student_id=task.student_id,
                    evidence_id=evidence.id,
                    knowledge_point_id=task.knowledge_point_id,
                )
            )
            event_id = event.id
        return {
            "item_index": state["item_index"] + 1,
            "followup_event_ids": list(dict.fromkeys([*state["followup_event_ids"], event_id])),
        }

    async def finish(self, state: RunState) -> dict:
        with Session(self.engine) as session:
            receipt_key = f"task:{state['task_id']}:finish"
            if receipt := session.get(NodeReceipt, receipt_key):
                return receipt.result
            task = self._task(session, state["task_id"])
            task.status = TaskStatus.DONE
            result = {"awaiting_item_id": None, "incoming_message": None}
            session.add(NodeReceipt(key=receipt_key, result=result))
            self._queue_task(session, state)
            session.commit()
            await self._publish_task(task)
        return result

    async def replan(self, state: RunState) -> dict:
        self._require("planner")
        reason = state["tutor_turn"]["replan_reason"]
        if not reason:
            raise ValueError("申请重规划时必须说明原因")
        with Session(self.engine) as session:
            receipt_key = f"task:{state['task_id']}:replan"
            if receipt := session.get(NodeReceipt, receipt_key):
                return receipt.result
            task = self._task(session, state["task_id"])
            task.status = TaskStatus.CANCELLED
            task.reason = f"{task.reason}\n重规划：{reason}"
            # 这是现有任务到期/待调度信号；原因保存在任务中，规划 Agent 可直接读取。
            event = make_event(
                TaskDue(task_id=task.id),
                date.fromisoformat(state["teaching_date"]),
                task.student_id,
            )
            session.add(event)
            session.flush()
            result = {
                "awaiting_item_id": None,
                "followup_event_ids": [*state["followup_event_ids"], event.id],
                "replan_event_id": event.id,
            }
            session.add(NodeReceipt(key=receipt_key, result=result))
            queue_work(session, f"event:{event.id}", {"dispatch": False})
            self._queue_task(session, state)
            session.commit()
            await self._publish_task(task)
        return result
