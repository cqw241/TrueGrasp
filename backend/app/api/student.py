"""学生辅导台。辅导回复通过 ``GET /api/events`` 流式推送。"""

from fastapi import APIRouter, Request, status

from app.api.stub import not_implemented
from app.contracts.api import Accepted, ChatTurn, MessageCreate
from app.contracts.entities import Student, Task

router = APIRouter(tags=["student"])


@router.get("/students")
def list_students() -> list[Student]:
    not_implemented("T19")


@router.get("/tasks/{task_id}")
def get_task(task_id: int) -> Task:
    not_implemented("T08")


@router.post("/tasks/{task_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_task(task_id: int, request: Request) -> Accepted:
    """学生打开任务卡，辅导 Agent 先发言。"""
    await request.app.state.orchestrator.enqueue_start(task_id)
    return Accepted(task_id=task_id)


@router.get("/tasks/{task_id}/messages")
async def list_messages(task_id: int, request: Request) -> list[ChatTurn]:
    return await request.app.state.orchestrator.get_messages(task_id)


@router.post("/tasks/{task_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def post_message(task_id: int, body: MessageCreate, request: Request) -> Accepted:
    await request.app.state.orchestrator.enqueue_message(task_id, body)
    return Accepted(task_id=task_id)
