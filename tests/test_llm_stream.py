"""llm 流式收包与空内容/JSON 失败重试（不联网）。"""
from __future__ import annotations

import json

import httpx
import pytest

from polysage import llm
from polysage.config import settings


def _sse(chunks: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks] + ["data: [DONE]"]
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture
def stream_client(monkeypatch):
    """把 llm 里的 httpx.Client 换成返回固定 SSE 的假客户端；返回“最近一次请求体”的容器。"""
    seen: dict = {}

    def install(chunks: list[dict], status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            seen["json"] = json.loads(request.content)
            return httpx.Response(status, content=_sse(chunks), headers={"content-type": "text/event-stream"})

        real = httpx.Client
        monkeypatch.setattr(llm.httpx, "Client", lambda *a, **kw: real(transport=httpx.MockTransport(handler), timeout=kw.get("timeout")))
        return seen

    monkeypatch.setattr(settings, "sf_api_key", "fake")
    monkeypatch.setattr(settings, "llm_profile", "siliconflow")
    return install


def test_stream_assembles_content_tool_calls_and_usage(stream_client):
    seen = stream_client([
        {"choices": [{"delta": {"reasoning_content": "想一下"}}]},
        {"choices": [{"delta": {"content": "好的，"}}]},
        {"choices": [{"delta": {"content": "登记。"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "add_price", "arguments": "{\"code\": \"LL\""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ", \"price\": 8600}"}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"completion_tokens": 12}},
    ])
    res = llm.chat([{"role": "user", "content": "x"}], max_tokens=100, stream=True)
    assert seen["json"]["stream"] is True and seen["json"]["max_tokens"] == 100
    assert res.content == "好的，登记。"
    assert res.tool_calls == [{"id": "c1", "name": "add_price", "arguments": {"code": "LL", "price": 8600}}]
    assert res.finish_reason == "tool_calls" and res.usage["completion_tokens"] == 12


def test_thinking_budget_adds_to_max_tokens_and_zju_uses_template_kwargs(stream_client, monkeypatch):
    seen = stream_client([{"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}])
    llm.chat([{"role": "user", "content": "x"}], max_tokens=1500, thinking=1024)
    assert seen["json"]["max_tokens"] == 2524 and seen["json"]["thinking_budget"] == 1024
    monkeypatch.setattr(settings, "llm_profile", "zju")
    monkeypatch.setattr(settings, "zju_api_key", "fake")
    monkeypatch.setattr(settings, "zju_supports_thinking", True)
    llm.chat([{"role": "user", "content": "x"}], max_tokens=100, thinking=1024)
    assert seen["json"]["chat_template_kwargs"] == {"enable_thinking": False} and "enable_thinking" not in seen["json"]
    llm.chat([{"role": "user", "content": "x"}], max_tokens=100, thinking=True)
    assert seen["json"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_empty_content_retries_without_thinking(monkeypatch):
    monkeypatch.setattr(settings, "sf_api_key", "fake")
    monkeypatch.setattr(settings, "llm_profile", "siliconflow")
    calls: list[dict] = []

    def fake_stream(payload, **kw):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "", "reasoning_content": "思考被截断"}, "finish_reason": "length"}]}
        return {"choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(llm, "_post_stream", fake_stream)
    out = llm.chat_json([{"role": "user", "content": "x"}], max_tokens=300, thinking=512)
    assert out == {"ok": True}
    assert len(calls) == 2 and calls[1]["enable_thinking"] is False and calls[1]["max_tokens"] == 600


def test_chat_json_retries_on_unparsable(monkeypatch):
    monkeypatch.setattr(settings, "sf_api_key", "fake")
    monkeypatch.setattr(settings, "llm_profile", "siliconflow")
    answers = iter(["这不是 JSON", "{\"a\": 1}"])
    seen: list[list[dict]] = []

    def fake_chat(messages, **kw):
        seen.append(messages)
        return llm.ChatResult(content=next(answers))

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert llm.chat_json([{"role": "user", "content": "x"}]) == {"a": 1}
    assert len(seen) == 2 and "只输出一个 JSON" in seen[1][-1]["content"]
