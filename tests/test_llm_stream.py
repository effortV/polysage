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


def test_stream_retries_on_ssl_eof_then_falls_back_to_non_stream(monkeypatch):
    import ssl

    monkeypatch.setattr(settings, "sf_api_key", "fake")
    monkeypatch.setattr(settings, "llm_profile", "siliconflow")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    attempts: list[str] = []

    def bad_stream(url, payload, timeout):
        attempts.append("stream")
        raise ssl.SSLError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")

    def ok_raw(path, payload, timeout, retries, kind):
        attempts.append("raw")
        return {"choices": [{"message": {"content": "{\"ok\": 1}"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(llm, "_stream_once", bad_stream)
    monkeypatch.setattr(llm, "_post_raw", ok_raw)
    assert llm.chat_json([{"role": "user", "content": "x"}]) == {"ok": 1}
    assert attempts == ["stream", "stream", "stream", "raw"]
    assert any("模型请求重试" in e["label"] for e in llm.activity.snapshot()["events"])


def test_chat_answer_falls_back_when_empty(monkeypatch):
    """模型空手而回：先换问法，再换另一个模型，最后也要给可读提示。"""
    monkeypatch.setattr(settings, "sf_api_key", "fake")
    monkeypatch.setattr(settings, "zju_api_key", "fake")
    monkeypatch.setattr(settings, "llm_profile", "zju")
    seen: list[tuple[str, int]] = []

    def fake_chat(messages, **kw):
        seen.append((settings.llm_profile, len(messages)))
        if settings.llm_profile == "zju":
            return llm.ChatResult(content="")                    # ZJU 一直空
        return llm.ChatResult(content="这是备用模型给出的结论。")

    monkeypatch.setattr(llm, "chat", fake_chat)
    res = llm.chat_answer([{"role": "user", "content": "出方案"}], tools=[{"x": 1}], max_tokens=1000)
    assert "备用模型" in res.content and "结论" in res.content
    assert [p for p, _ in seen] == ["zju", "zju", "siliconflow"]
    assert settings.llm_profile == "zju"                          # 用完要还原

    monkeypatch.setattr(llm, "chat", lambda messages, **kw: llm.ChatResult(content=""))
    assert "连续返回空内容" in llm.chat_answer([{"role": "user", "content": "x"}]).content


def test_reasoning_is_not_used_as_answer(monkeypatch):
    """模型只给了内部思考（reasoning_content）时，不能当成答案返回。"""
    monkeypatch.setattr(settings, "sf_api_key", "fake")
    monkeypatch.setattr(settings, "llm_profile", "siliconflow")
    monkeypatch.setattr(llm, "_post_stream", lambda payload, **kw: {
        "choices": [{"message": {"content": "", "reasoning_content": "Hmm, let me think about this..."}, "finish_reason": "stop"}]})
    monkeypatch.setattr(llm, "_post", lambda *a, **kw: {
        "choices": [{"message": {"content": "", "reasoning_content": "Hmm..."}, "finish_reason": "stop"}]})
    res = llm.chat([{"role": "user", "content": "出方案"}], max_tokens=500)
    assert res.content == "" and "Hmm" not in res.content


def test_truncated_answer_is_continued(monkeypatch):
    """被 max_tokens 掐断（finish_reason=length）时接着写完，而不是给用户半张表。"""
    monkeypatch.setattr(settings, "sf_api_key", "fake")
    monkeypatch.setattr(settings, "llm_profile", "siliconflow")
    calls = {"n": 0}

    def fake_chat(messages, **kw):
        calls["n"] += 1
        assert "tools" not in kw                       # 续写时不该再带工具
        if calls["n"] == 1:
            return llm.ChatResult(content="| 排名 | 配方 | 成本 |\n| 1 | LL 20 / R1 50 |", finish_reason="length")
        if calls["n"] == 2:
            return llm.ChatResult(content=" 6800 |\n| 2 | LL 25 / R1 45 |", finish_reason="length")
        return llm.ChatResult(content=" 6900 |\n\n以上为完整表格。", finish_reason="stop")

    monkeypatch.setattr(llm, "chat", fake_chat)
    first = fake_chat([{"role": "user", "content": "给我表格"}])
    res = llm.continue_if_truncated(first, [{"role": "user", "content": "给我表格"}], max_tokens=100)
    assert res.content.endswith("以上为完整表格。") and "6800" in res.content and "6900" in res.content
    assert res.finish_reason == "stop" and calls["n"] == 3

    done = llm.ChatResult(content="写完了。", finish_reason="stop")
    assert llm.continue_if_truncated(done, [{"role": "user", "content": "x"}]).content == "写完了。"
