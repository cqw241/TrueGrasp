"""模型接入层（T06），实现 app.contracts.llm.LLMClient。

连接参数来自 Settings：llm_base_url、llm_api_key、llm_model。
"""

from collections.abc import AsyncIterator

from pydantic import BaseModel

from app.config import Settings
from app.contracts.llm import ChatMessage, Tool


class OpenAICompatClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def generate[T: BaseModel](
        self,
        messages: list[ChatMessage],
        output: type[T],
        tools: list[Tool] | None = None,
    ) -> T:
        raise NotImplementedError("T06")

    def stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        raise NotImplementedError("T06")
