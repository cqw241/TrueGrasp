"""学生辅导台。辅导回复通过 ``GET /api/events`` 流式推送。"""

from base64 import b64encode
from collections.abc import Awaitable, Callable
from io import BytesIO
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from openai import OpenAIError
from PIL import Image
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.stub import not_implemented
from app.config import get_settings
from app.contracts.api import Accepted, AnswerTarget, ChatTurn, MessageCreate, QuestionCreate
from app.contracts.entities import Student, Task
from app.llm.client import LLMOutputError, OpenAICompatClient
from app.llm.vision import Transcription, transcribe_answer
from app.verify.parsing import parse_answer

router = APIRouter(tags=["student"])
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTO_PIXELS = 24_000_000
PHOTO_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def _is_photo(data: bytearray, mime: str) -> bool:
    try:
        with Image.open(BytesIO(data), formats=[PHOTO_FORMATS[mime]]) as image:
            if image.width * image.height > MAX_PHOTO_PIXELS or getattr(
                image, "is_animated", False
            ):
                return False
            image.verify()
        # verify() alone does not check whether the pixel data is decodable.
        with Image.open(BytesIO(data), formats=[PHOTO_FORMATS[mime]]) as image:
            image.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return False
    return True


UNPARSABLE_DOUBT = "识别结果无法解析为公式，请核对后再提交"


def _parses(latex: str) -> bool:
    try:
        parse_answer(latex)
    except Exception:  # noqa: BLE001 - SymPy 解析器可能抛出多种异常，一律视为无法解析
        return False
    return True


class PhotoUploadError(BaseModel):
    detail: str


@router.get("/students")
def list_students(request: Request) -> list[Student]:
    with Session(request.app.state.orchestrator.engine) as session:
        return list(session.exec(select(Student).order_by(Student.id)).all())


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


@router.get("/tasks/{task_id}/answer-target")
async def get_answer_target(task_id: int, request: Request) -> AnswerTarget:
    return await request.app.state.orchestrator.get_answer_target(task_id)


PHOTO_RESPONSES: dict[int | str, dict] = {
    413: {"model": PhotoUploadError, "description": "照片超过大小限制"},
    415: {"model": PhotoUploadError, "description": "不支持该图片类型"},
    422: {
        "description": "参数无效、图片无法解码或未识别到内容",
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {"$ref": "#/components/schemas/PhotoUploadError"},
                    ]
                }
            }
        },
    },
    502: {"model": PhotoUploadError, "description": "模型识别失败"},
    503: {"model": PhotoUploadError, "description": "图片识别服务未配置"},
}
PHOTO_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            mime: {"schema": {"type": "string", "format": "binary"}} for mime in PHOTO_FORMATS
        },
    },
}


async def _transcribe_upload(
    request: Request,
    ensure_current: Callable[[], Awaitable[None]] | None = None,
    *,
    empty_detail: str,
) -> Transcription:
    """读取请求体中的照片并转写为 LaTeX。``ensure_current`` 在每个耗时步骤之后复查前提。"""
    mime = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if mime not in PHOTO_FORMATS:
        raise HTTPException(415, "请上传 JPEG、PNG 或 WebP 图片")

    async def check() -> None:
        if ensure_current is not None:
            await ensure_current()

    await check()
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "照片不能超过 8 MB")
        data.extend(chunk)
    if not await run_in_threadpool(_is_photo, data, mime):
        raise HTTPException(422, "图片损坏、尺寸过大或无法识别，请重新拍摄或选择图片")
    await check()
    image = f"data:{mime};base64,{b64encode(data).decode('ascii')}"
    llm = getattr(request.app.state, "photo_llm", None)
    try:
        if llm is not None:
            result = await transcribe_answer(llm, [image])
        else:
            settings = get_settings()
            if settings.vision_llm_base_url and not (
                settings.vision_llm_api_key and settings.vision_llm_model
            ):
                raise HTTPException(503, "图片识别服务未配置完整")
            key = settings.vision_llm_api_key or settings.llm_api_key
            if not key:
                raise HTTPException(503, "图片识别服务未配置")
            base_url = settings.vision_llm_base_url or settings.llm_base_url
            try:
                address = urlsplit(base_url)
                if address.scheme not in {"http", "https"} or not address.hostname:
                    raise ValueError("invalid scheme or hostname")
                if address.port == 0:
                    raise ValueError("invalid port")
            except ValueError as exc:
                raise HTTPException(503, "图片识别服务地址配置无效") from exc
            vision = settings.model_copy(
                update={
                    "llm_base_url": base_url,
                    "llm_api_key": key,
                    "llm_model": settings.vision_llm_model or settings.llm_model,
                }
            )
            async with OpenAICompatClient(vision) as client:
                result = await transcribe_answer(client, [image])
    except (OpenAIError, LLMOutputError) as exc:
        raise HTTPException(502, "照片识别失败，请重试或手动输入") from exc
    await check()
    if not result.latex.strip():
        raise HTTPException(422, empty_detail)
    return result


@router.post(
    "/tasks/{task_id}/answer-photo",
    responses={
        404: {"model": PhotoUploadError, "description": "任务不存在"},
        409: {"model": PhotoUploadError, "description": "题目或作答轮次已变化"},
        **PHOTO_RESPONSES,
    },
    openapi_extra=PHOTO_BODY,
)
async def transcribe_photo(
    task_id: int,
    request: Request,
    item_id: str = Query(min_length=1, max_length=256),
    turn: int = Query(ge=0),
) -> Transcription:
    """识别当前题目的作答照片，返回待确认草稿，不提交作答。"""

    async def ensure_current() -> None:
        target = await request.app.state.orchestrator.get_answer_target(task_id)
        if target.phase != "waiting" or target.item_id != item_id or target.turn != turn:
            raise HTTPException(409, "题目已变化，请重新打开当前待答题")

    result = await _transcribe_upload(
        request, ensure_current, empty_detail="没有识别到作答，请拍清楚答案或手动输入"
    )
    # 技术文档第 8 节第 5 条：可解析且无疑问才直接进入诊断；解析失败记为疑问，交学生确认。
    if not await run_in_threadpool(_parses, result.latex):
        result.doubts = [*result.doubts, UNPARSABLE_DOUBT]
    return result


@router.post(
    "/students/{student_id}/question-photo",
    responses={404: {"model": PhotoUploadError, "description": "学生不存在"}, **PHOTO_RESPONSES},
    openapi_extra=PHOTO_BODY,
)
async def transcribe_question_photo(student_id: str, request: Request) -> Transcription:
    """识别学生要提问的题目照片，返回草稿供学生校对，不创建提问。"""
    with Session(request.app.state.orchestrator.engine) as session:
        if session.get(Student, student_id) is None:
            raise HTTPException(404, "学生不存在")
    return await _transcribe_upload(request, empty_detail="没有识别到题目，请拍清楚题目或手动输入")


@router.post("/students/{student_id}/questions", status_code=status.HTTP_202_ACCEPTED)
async def ask_question(student_id: str, body: QuestionCreate, request: Request) -> Task:
    """学生主动提问（技术文档第 5 节 ⑥）。

    创建一条 ``question`` 任务，辅导 Agent 先请学生尝试。之后的分层提示与完整解答作为
    该任务的对话推送，由 ``ChatTurn.hint_level`` 与 ``ChatTurn.solution`` 标注。
    """
    not_implemented("T15")


@router.post("/tasks/{task_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def post_message(
    task_id: int,
    body: MessageCreate,
    request: Request,
    expected_turn: int | None = Query(None, ge=0),
) -> Accepted:
    await request.app.state.orchestrator.enqueue_message(task_id, body, expected_turn=expected_turn)
    return Accepted(task_id=task_id)
