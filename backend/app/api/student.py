"""学生辅导台。辅导回复通过 ``GET /api/events`` 流式推送。"""

from fastapi import APIRouter, status

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
def start_task(task_id: int) -> Accepted:
    """学生打开任务卡，辅导 Agent 先发言。"""
    not_implemented("T03")


@router.get("/tasks/{task_id}/messages")
def list_messages(task_id: int) -> list[ChatTurn]:
    not_implemented("T03")


@router.post("/tasks/{task_id}/messages", status_code=status.HTTP_202_ACCEPTED)
def post_message(task_id: int, body: MessageCreate) -> Accepted:
    not_implemented("T03")
