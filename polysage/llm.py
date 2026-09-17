"""硅基流动（SiliconFlow）OpenAI 兼容客户端：对话 / 函数调用 / 向量 / 重排。

- 对话模型默认 deepseek-ai/DeepSeek-V4-Pro（可在 .env 修改）。
- DeepSeek-V4-Pro 默认带“思考”（reasoning）：一个小请求也要 60～80 s、数千 token。
  这里默认 enable_thinking=False（5 s 级），只在机理报告、复盘、推荐复核等需要推理的调用上按预算开启（thinking=<token 预算>）。
- 未配置密钥时：chat 抛出 LLMNotConfigured；embed/rerank 返回 None，上层自动退化为 BM25 检索。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx
import numpy as np

from .config import settings
from . import activity


class LLMNotConfigured(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _headers(kind: str = "chat") -> dict[str, str]:
    """kind: chat → 当前对话模型（按 LLM_PROFILE）；embed → 固定 SiliconFlow。"""
    key = settings.chat_api_key if kind == "chat" else settings.sf_api_key
    if not key:
        if kind == "chat":
            raise LLMNotConfigured(f"当前对话模型 {settings.chat_model}（{settings.llm_profile}）未配置密钥，请在“设置”页填写。")
        raise LLMNotConfigured("未配置 SILICONFLOW_API_KEY（向量/重排需要）。")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _base_url(kind: str = "chat") -> str:
    return (settings.chat_base_url if kind == "chat" else settings.sf_base_url).rstrip("/")


def _thinking_payload(thinking: bool | int | None) -> dict[str, Any]:
    """thinking: None/False = 关闭；True = 模型默认（完整思考）；int = 思考 token 预算。不支持的服务不发该参数。"""
    if not settings.chat_supports_thinking:
        return {}
    if thinking is None or thinking is False:
        return {"enable_thinking": False}
    if thinking is True:
        return {}
    return {"enable_thinking": True, "thinking_budget": int(thinking)}


def _post(path: str, payload: dict[str, Any], timeout: float = 300.0, retries: int = 3, kind: str = "chat") -> dict[str, Any]:
    with activity.track("llm" if kind == "chat" else kind, payload.get("model", "")):
        return _post_raw(path, payload, timeout, retries, kind)


def _post_raw(path: str, payload: dict[str, Any], timeout: float, retries: int, kind: str) -> dict[str, Any]:
    url = _base_url(kind) + path
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(url, headers=_headers(kind), json=payload)
            if r.status_code == 429 or r.status_code >= 500:
                last = LLMError(f"{r.status_code}: {r.text[:300]}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"{settings.llm_profile if kind == 'chat' else 'SiliconFlow'} {r.status_code}: {r.text[:500]}")
            return r.json()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as e:
            # 超时 / 断连 / 代理掐断：退避重试
            last = e
            time.sleep(2.0 * (attempt + 1))
    raise LLMError(f"LLM 请求失败（{_base_url(kind)}）: {last}")


def chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    json_mode: bool = False,
    model: str | None = None,
    thinking: bool | int | None = None,
) -> ChatResult:
    payload: dict[str, Any] = {
        "model": model or settings.chat_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        **_thinking_payload(thinking),
    }
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    data = _post("/chat/completions", payload)
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            args = {"_raw": args_raw}
        tool_calls.append({"id": tc.get("id") or f"call_{len(tool_calls)}", "name": fn.get("name"), "arguments": args})
    return ChatResult(
        content=msg.get("content") or "",
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason") or "",
        usage=data.get("usage") or {},
        raw=data,
    )


def chat_stream(messages: list[dict[str, Any]], *, temperature: float = 0.3, max_tokens: int = 4096,
                model: str | None = None, thinking: bool | int | None = None) -> Iterator[str]:
    """流式输出纯文本（不含工具调用），供界面逐字显示。"""
    url = _base_url("chat") + "/chat/completions"
    payload = {"model": model or settings.chat_model, "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "stream": True, **_thinking_payload(thinking)}
    with activity.track("llm", payload["model"]), httpx.Client(timeout=180.0) as client:
        with client.stream("POST", url, headers=_headers("chat"), json=payload) as r:
            if r.status_code >= 400:
                raise LLMError(f"SiliconFlow {r.status_code}: {r.read()[:500]!r}")
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue
                delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                piece = delta.get("content")
                if piece:
                    yield piece


def chat_json(messages: list[dict[str, Any]], *, temperature: float = 0.2, max_tokens: int = 4096,
              thinking: bool | int | None = None) -> Any:
    """要求模型返回 JSON 并解析；解析失败时尝试截取首尾大括号。默认不开思考（结构化抽取任务不需要）。"""
    res = chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=True, thinking=thinking)
    return parse_json_loose(res.content)


def parse_json_loose(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"模型未返回可解析的 JSON：{text[:200]}")


def embed(texts: list[str], batch: int = 32) -> np.ndarray | None:
    """返回 (n, d) float32；未配置密钥返回 None。"""
    if not settings.sf_api_key or not texts:
        return None
    vecs: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = [t[:6000] for t in texts[i:i + batch]]
        data = _post("/embeddings", {"model": settings.sf_embedding_model, "input": part, "encoding_format": "float"}, kind="embed")
        items = sorted(data.get("data") or [], key=lambda x: x.get("index", 0))
        vecs.extend(it["embedding"] for it in items)
    arr = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def rerank(query: str, docs: list[str], top_n: int | None = None) -> list[tuple[int, float]] | None:
    """返回 [(doc_index, score)] 按分数降序；未配置密钥返回 None。"""
    if not settings.sf_api_key or not docs:
        return None
    payload = {"model": settings.sf_rerank_model, "query": query, "documents": [d[:4000] for d in docs],
               "top_n": top_n or len(docs), "return_documents": False}
    try:
        data = _post("/rerank", payload, timeout=60.0, retries=2, kind="embed")
    except LLMError:
        return None
    results = data.get("results") or []
    return [(int(r["index"]), float(r.get("relevance_score", 0.0))) for r in results]


def ping() -> tuple[bool, str]:
    """设置页“测试连接”。"""
    try:
        res = chat([{"role": "user", "content": "回复“OK”两个字母即可。"}], max_tokens=8, temperature=0, thinking=False)
        return True, f"模型 {settings.chat_model}（{settings.chat_base_url}）可用：{res.content.strip()[:20]}"
    except LLMNotConfigured as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"连接失败：{e}"
