"""模型接入层的契约，由 app.llm 实现（T06）。

三个 Agent 都通过 LLMClient 调用同一个模型；Agent 不直接使用 OpenAI SDK。
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Tool:
    """供模型调用的工具。模型给出的参数先按 ``args`` 校验，再交给 ``handler``。

    handler 的返回值序列化为 JSON 后回传给模型。
    """

    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[[Any], BaseModel | dict[str, Any] | str]


class LLMClient(Protocol):
    async def generate[T: BaseModel](
        self,
        messages: list[ChatMessage],
        output: type[T],
        tools: list[Tool] | None = None,
    ) -> T:
        """结构化输出：按 ``output`` 的 JSON Schema 生成并校验，校验失败时重试。

        传入 ``tools`` 时，先完成工具调用循环，再给出最终的结构化结果。
        """
        ...

    def stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        """流式文本，逐段产出，用于辅导对话。"""
        ...
