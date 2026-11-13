import asyncio
import json
import logging
import time

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings
from app.contracts.llm import ChatMessage, Tool
from app.llm.client import LLMOutputError, OpenAICompatClient
from app.llm.math_tools import math_tools
from app.llm.prompts import PromptTemplates
from app.llm.vision import transcribe_answer
from app.verify.tools import SympyMathTools


class Output(BaseModel):
    answer: str


def completion(content=None, calls=None, reason="stop", refusal=None):
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 0,
        "model": "configured",
        "choices": [
            {
                "index": 0,
                "finish_reason": reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": calls,
                    "refusal": refusal,
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def call(name="check_equivalent", args=None):
    return {
        "id": "call1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                args or {"a": "x+x", "b": "2x", "mode": "expr", "variables": ["x"]}
            ),
        },
    }


async def scenario(responses, operation, **settings):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = responses.pop(0)
        return httpx.Response(200, json=response)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        AsyncOpenAI(api_key="private-test-key", http_client=http) as sdk,
    ):
        client = OpenAICompatClient(
            Settings(_env_file=None, llm_model="configured", **settings), client=sdk
        )
        result = await operation(client)
    return result, requests


@pytest.mark.parametrize("role", ["planner", "tutor", "verifier"])
def test_structured_retry_isolation_and_metrics(role, caplog):
    caplog.set_level(logging.INFO)
    messages = [ChatMessage(role="user", content=role)]

    async def operation(client):
        result = await client.generate(messages, Output)
        assert len(messages) == 1
        return result

    result, requests = asyncio.run(
        scenario(
            [
                completion("{}"),
                completion('{"answer":"ok"}'),
            ],
            operation,
            llm_response_format="json_schema",
        )
    )
    assert result.answer == "ok"
    assert requests[0]["model"] == "configured"
    assert requests[0]["response_format"]["json_schema"]["schema"]["required"] == ["answer"]
    assert len(requests[1]["messages"]) > len(requests[0]["messages"])
    assert "elapsed_ms" in caplog.text and "total_tokens" in caplog.text
    assert "private-test-key" not in caplog.text


@pytest.mark.parametrize(
    "tool_call,error", [(call(), False), (call("unknown"), True), (call(args={"a": 2}), True)]
)
def test_tool_loop(tool_call, error):
    async def operation(client):
        return await client.generate([], Output, math_tools(SympyMathTools()))

    result, requests = asyncio.run(
        scenario(
            [
                completion(calls=[tool_call], reason="tool_calls"),
                completion('{"answer":"ok"}'),
            ],
            operation,
        )
    )
    payload = json.loads(requests[1]["messages"][-1]["content"])
    assert ("error" in payload) is error
    if not error:
        assert payload["equivalent"] is True
    assert result.answer == "ok"


@pytest.mark.parametrize(
    "response", [completion("bad"), completion("{}", reason="length"), completion(refusal="no")]
)
class ToolArgs(BaseModel):
    value: str


@pytest.mark.parametrize("mode", ["timeout", "exception"])
def test_tool_failure_is_returned_to_the_model(mode):
    def handler(args):
        if mode == "timeout":
            time.sleep(0.2)
            return {"ok": True}
        raise ValueError("boom")

    tool = Tool("slow", "slow tool", ToolArgs, handler)
    tool_call = {
        "id": "call-slow",
        "type": "function",
        "function": {"name": "slow", "arguments": json.dumps({"value": "x"})},
    }

    async def operation(client):
        return await client.generate([], Output, [tool])

    result, requests = asyncio.run(
        scenario(
            [
                completion(calls=[tool_call], reason="tool_calls"),
                completion('{"answer":"ok"}'),
            ],
            operation,
            math_tool_timeout=0.01,
        )
    )
    payload = json.loads(requests[1]["messages"][-1]["content"])
    assert "error" in payload
    assert result.answer == "ok"


@pytest.mark.parametrize(
    "response", [completion("bad"), completion("{}", reason="length"), completion(refusal="no")]
)
def test_failure_is_explicit(response):
    async def operation(client):
        return await client.generate([], Output)

    with pytest.raises(LLMOutputError):
        asyncio.run(scenario([response], operation, llm_validation_retries=0))


def test_tool_budget():
    async def operation(client):
        return await client.generate([], Output, math_tools(SympyMathTools()))

    with pytest.raises(LLMOutputError, match="Tool round"):
        asyncio.run(
            scenario(
                [completion(calls=[call()]), completion(calls=[call()])],
                operation,
                llm_max_tool_rounds=1,
            )
        )


def test_vision_and_templates():
    async def operation(client):
        return await transcribe_answer(client, ["data:image/png;base64,dGVzdA=="])

    result, requests = asyncio.run(
        scenario(
            [
                completion(
                    json.dumps(
                        {
                            "latex": "x^2",
                            "doubts": ["exponent unclear"],
                            "requires_confirmation": False,
                        }
                    )
                ),
            ],
            operation,
        )
    )
    assert result.requires_confirmation and result.doubts
    assert requests[0]["messages"][-1]["content"][1]["type"] == "image_url"
    templates = PromptTemplates({"tutor": "Teach $topic; JSON: {}"})
    assert templates.render("tutor", topic="limits").content == "Teach limits; JSON: {}"
    with pytest.raises(KeyError):
        templates.render("tutor")


@pytest.mark.parametrize("complete", [True, False])
def test_stream(complete):
    async def operation():
        def handler(request):
            chunks = [
                {
                    "id": "s",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "configured",
                    "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
                },
                {
                    "id": "s",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "configured",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop" if complete else None}
                    ],
                },
            ]
            body = (
                "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                + "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
            AsyncOpenAI(api_key="test", http_client=http) as sdk,
        ):
            client = OpenAICompatClient(Settings(_env_file=None), client=sdk)
            if complete:
                assert [token async for token in client.stream([])] == ["hello"]
            else:
                with pytest.raises(LLMOutputError, match="completion marker"):
                    [token async for token in client.stream([])]

    asyncio.run(operation())
