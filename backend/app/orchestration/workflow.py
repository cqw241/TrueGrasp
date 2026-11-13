"""给作业发布服务使用的内部状态投影，不增加发布入口或另一套任务系统。"""

from typing import Literal

from pydantic import BaseModel
from sqlmodel import Session, select

from app.contracts.entities import Event, Task
from app.contracts.enums import EventType
from app.contracts.events import HomeworkImported, read_payload
from app.orchestration.storage import EventTaskLink, NodeReceipt, PendingWork, WorkFailure


class HomeworkWorkflow(BaseModel):
    event_id: int
    state: Literal["pending", "planned", "failed"]
    task_ids: list[int]
    error: str | None = None


def homework_event_ids(session: Session, event_id: int, task_ids: list[int]) -> set[int]:
    """包括这些任务的作答/到期事件，失败的后续规划也须回到原作业。"""
    event = session.get(Event, event_id)
    return {
        event_id,
        *session.exec(
            select(Event.id).where(
                Event.type.in_([EventType.ANSWER_SUBMITTED, EventType.TASK_DUE]),
                Event.class_id == event.class_id,
                Event.payload["task_id"].as_integer().in_(task_ids),
            )
        ).all(),
    }


def homework_workflow(session: Session, event_id: int) -> HomeworkWorkflow:
    """只投影已提交的事实；调用方须先做教师/班级授权，勿直接对公网暴露。"""
    event = session.get(Event, event_id)
    if event is None or not isinstance(read_payload(event), HomeworkImported):
        raise ValueError("作业导入事件不存在")
    receipt = session.get(NodeReceipt, f"event:{event_id}:save_plan")
    task_ids = set(
        session.exec(select(EventTaskLink.task_id).where(EventTaskLink.event_id == event_id)).all()
    )
    # 兼容增加来源表之前已部署的规划回执。
    if receipt is not None:
        task_ids.update(receipt.result.get("task_ids", []))
    task_ids = set(
        session.exec(select(Task.id).where(Task.id.in_(task_ids), Task.class_id == event.class_id))
    )
    event_ids = homework_event_ids(session, event_id, sorted(task_ids))
    work_keys = [*(f"event:{i}" for i in event_ids), *(f"task:{i}" for i in task_ids)]
    failure = session.exec(
        select(WorkFailure)
        .join(PendingWork, WorkFailure.key == PendingWork.key)
        .where(WorkFailure.key.in_(work_keys), PendingWork.pending)
        .order_by(WorkFailure.failed_at.desc())
    ).first()
    error = None
    if failure is not None:
        state = "failed"
        error = f"{failure.key} 执行失败，保留已导入作业与当前进度，请重试原工作。"
    else:
        unprocessed = session.exec(
            select(Event.id).where(Event.id.in_(event_ids), Event.processed == False)  # noqa: E712
        ).first()
        state = "planned" if unprocessed is None else "pending"
    return HomeworkWorkflow(event_id=event_id, state=state, task_ids=sorted(task_ids), error=error)
