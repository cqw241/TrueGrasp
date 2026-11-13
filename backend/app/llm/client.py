"""模型接入层（T06），实现 app.contracts.llm.LLMClient。

连接参数来自 Settings：llm_base_url、llm_api_key、llm_model。
"""

import inspect
import json
import logging
from collections.abc import AsyncIterator
from time import perf_counter

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.contracts.llm import ChatMessage, Tool
from app.verify.execution import run_with_timeout

logger = logging.getLogger(__name__)


class LLMOutputError(RuntimeError):
    """Provider refused, truncated, or exhausted the validation/tool budget."""


class OpenAICompatClient:
    def __init__(self, settings: Settings, *, client: AsyncOpenAI | None = None) -> None:
        self.settings = settings
        self._client = client or AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
            max_retries=2,
        )
        self._owns_client = client is None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()

    def _record(self, started, usage, status):
        logger.info(
            "llm_request model=%s elapsed_ms=%.1f status=%s usage=%s",
            self.settings.llm_model,
            (perf_counter() - started) * 1000,
            status,
            usage.model_dump() if usage else None,
        )

    async def generate[T: BaseModel](
        self,
        messages: list[ChatMessage],
        output: type[T],
        tools: list[Tool] | None = None,
    ) -> T:
        registry = {tool.name: tool for tool in tools or []}
        if len(registry) != len(tools or []):
            raise ValueError("Tool names must be unique")
        schema = output.model_json_schema()
        history = [message.model_dump(mode="json") for message in messages]
        history.insert(
            0,
            {
                "role": "system",
                "content": "Return JSON matching this schema: " + json.dumps(schema),
            },
        )
        response_format = {"type": "json_object"}
        if self.settings.llm_response_format == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": output.__name__, "schema": schema},
            }
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.args.model_json_schema(),
                },
            }
            for t in registry.values()
        ]
        failures = rounds = 0
        while True:
            started, usage, status = perf_counter(), None, "error"
            try:
                response = await self._client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=history,
                    response_format=response_format,
                    **({"tools": definitions} if definitions else {}),
                )
                usage = response.usage
                if not response.choices:
                    raise LLMOutputError("Provider returned no choices")
                choice = response.choices[0]
                message = choice.message
                if message.refusal or choice.finish_reason in ("length", "content_filter"):
                    raise LLMOutputError("Provider refused or returned an incomplete response")
                if message.tool_calls:
                    rounds += 1
                    if rounds > self.settings.llm_max_tool_rounds:
                        raise LLMOutputError("Tool round budget exhausted")
                    history.append(message.model_dump(exclude_none=True))
                    for call in message.tool_calls:
                        tool = registry.get(call.function.name)
                        try:
                            if tool is None:
                                raise ValueError("Unknown tool")
                            args = tool.args.model_validate_json(call.function.arguments)
                        except (ValidationError, ValueError):
                            result = {
                                "error": "Unknown tool or invalid arguments; use its JSON schema"
                            }
                        else:
                            try:
                                result = await run_with_timeout(
                                    tool.handler,
                                    args,
                                    timeout=self.settings.math_tool_timeout,
                                )
                                if inspect.isawaitable(result):
                                    result = await result
                            except TimeoutError:
                                logger.warning("tool timed out name=%s", tool.name)
                                result = {"error": "工具超时；不要重复调用，改用文字继续解释"}
                            except Exception as exc:
                                logger.warning(
                                    "tool failed name=%s error=%s",
                                    tool.name,
                                    type(exc).__name__,
                                )
                                result = {"error": "工具执行失败；不要重复调用，改用文字继续解释"}
                        content = (
                            result.model_dump_json()
                            if isinstance(result, BaseModel)
                            else json.dumps(result, ensure_ascii=False, allow_nan=False)
                        )
                        history.append(
                            {"role": "tool", "tool_call_id": call.id, "content": content}
                        )
                    status = "tool"
                    continue
                try:
                    result = output.model_validate_json(message.content or "")
                except ValidationError as exc:
                    failures += 1
                    if failures > self.settings.llm_validation_retries:
                        raise LLMOutputError(
                            "Structured output validation budget exhausted"
                        ) from exc
                    history.append({"role": "assistant", "content": message.content or ""})
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "JSON schema validation failed. Return a complete JSON object "
                                "conforming to the supplied schema."
                            ),
                        }
                    )
                    status = "validation_retry"
                    continue
                status = "ok"
                return result
            finally:
                self._record(started, usage, status)

    async def stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        started, usage, status = perf_counter(), None, "error"
        stream = None
        finished = False
        try:
            stream = await self._client.chat.completions.create(
                model=self.settings.llm_model,
                messages=[message.model_dump(mode="json") for message in messages],
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason == "stop":
                    finished = True
                if choice.delta.refusal or choice.finish_reason in ("length", "content_filter"):
                    raise LLMOutputError("Incomplete or refused text stream")
                if choice.delta.content:
                    yield choice.delta.content
            if not finished:
                raise LLMOutputError("Stream ended without a completion marker")
            status = "ok"
        finally:
            if stream is not None:
                await stream.close()
            self._record(started, usage, status)
