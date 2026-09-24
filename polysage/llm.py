"""硅基流动（SiliconFlow）OpenAI 兼容客户端：对话 / 函数调用 / 向量 / 重排。

- 对话模型默认 deepseek-ai/DeepSeek-V4-Pro（可在 .env 修改）。
- DeepSeek-V4-Pro 默认带“思考”（reasoning）：一个小请求也要 60～80 s、数千 token。
  这里默认 enable_thinking=False（5 s 级），只在机理报告、复盘、推荐复核等需要推理的调用上按预算开启（thinking=<token 预算>）。
- 未配置密钥时：chat 抛出 LLMNotConfigured；embed/rerank 返回 None，上层自动退化为 BM25 检索。
"""
from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx
import numpy as np

from .config import settings
from . import activity, net


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
    """thinking: None/False = 关闭；True = 模型默认（完整思考）；int = 思考 token 预算。不支持的服务不发该参数。

    ZJU（vLLM 部署的 Qwen）不认顶层 enable_thinking，只认 chat_template_kwargs，而且不能限预算：
    不限预算的思考动辄上百秒、还会把 max_tokens 吃光导致答案为空，所以 ZJU 上只有 thinking=True 才开思考。
    """
    if not settings.chat_supports_thinking:
        return {}
    if settings.llm_profile == "zju":
        return {"chat_template_kwargs": {"enable_thinking": thinking is True}}
    if thinking is None or thinking is False:
        return {"enable_thinking": False}
    if thinking is True:
        return {}
    return {"enable_thinking": True, "thinking_budget": int(thinking)}


def _effective_max_tokens(max_tokens: int, thinking: bool | int | None) -> int:
    """部分服务把思考 token 计入 max_tokens；给了思考预算就把预算加到上限里，避免答案被截空。"""
    if isinstance(thinking, int) and not isinstance(thinking, bool):
        return max_tokens + int(thinking)
    return max_tokens


class _Retryable(Exception):
    """429 / 5xx：可重试。"""


def _stream_once(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """流式请求，拼成与非流式相同的响应结构（content / reasoning_content / tool_calls / usage）。"""
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish, usage = "", {}
    with net.client(url, timeout=httpx.Timeout(timeout, connect=30.0)) as client:
        with client.stream("POST", url, headers=_headers("chat"), json=payload) as r:
            if r.status_code == 429 or r.status_code >= 500:
                raise _Retryable(f"{r.status_code}: {r.read()[:300]!r}")
            if r.status_code >= 400:
                raise LLMError(f"{settings.llm_profile} {r.status_code}: {r.read()[:500]!r}")
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
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    elif delta.get("reasoning"):
                        reasoning.append(delta["reasoning"])
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index") or 0)
                        cur = calls.setdefault(idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            cur["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name") and not cur["function"]["name"]:
                            cur["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            cur["function"]["arguments"] += fn["arguments"]
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning)}
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}


def _post_stream(payload: dict[str, Any], timeout: float = 600.0, retries: int = 4) -> dict[str, Any]:
    """对话请求走流式：边生成边收，网关（Cloudflare 100 s）不会因为长时间没字节而 524。

    网络抖动（SSL EOF、连接被掐）重试；最后一次改走非流式，换一条路径。
    """
    url = _base_url("chat") + "/chat/completions"
    stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    last: Exception | None = None
    with activity.track("llm", payload.get("model", "")):
        for attempt in range(retries):
            try:
                if attempt == retries - 1:
                    return _post_raw("/chat/completions", payload, timeout, 1, "chat")
                return _stream_once(url, stream_payload, timeout)
            except _Retryable as e:
                last = LLMError(str(e))
                activity.note(f"模型请求重试 {attempt + 1}/{retries}：HTTP {str(e)[:3]}", category="llm", ok=False)
                time.sleep(1.5 * (attempt + 1))
            except LLMError as e:
                if attempt == retries - 1 or "请求失败" not in str(e):
                    raise
                last = e
            except _NET_ERRORS as e:
                last = e
                activity.note(f"模型请求重试 {attempt + 1}/{retries}：{type(e).__name__}", category="llm", ok=False)
                time.sleep(2.0 * (attempt + 1))
    raise LLMError(f"LLM 请求失败（{_base_url('chat')}）: {last}")


# 网络层可重试的异常：httpx 的超时/断连/协议错误，以及被中间设备掐断时冒出来的裸 SSL / OS 错误
_NET_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, ssl.SSLError, ConnectionError, OSError)


def _post(path: str, payload: dict[str, Any], timeout: float = 300.0, retries: int = 3, kind: str = "chat") -> dict[str, Any]:
    with activity.track("llm" if kind == "chat" else kind, payload.get("model", "")):
        return _post_raw(path, payload, timeout, retries, kind)


def _post_raw(path: str, payload: dict[str, Any], timeout: float, retries: int, kind: str) -> dict[str, Any]:
    url = _base_url(kind) + path
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with net.client(url, timeout=timeout) as client:
                r = client.post(url, headers=_headers(kind), json=payload)
            if r.status_code == 429 or r.status_code >= 500:
                last = LLMError(f"{r.status_code}: {r.text[:300]}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"{settings.llm_profile if kind == 'chat' else 'SiliconFlow'} {r.status_code}: {r.text[:500]}")
            return r.json()
        except _NET_ERRORS as e:
            # 超时 / 断连 / SSL EOF / 代理掐断：退避重试
            last = e
            activity.note(f"模型请求重试 {attempt + 1}/{retries}：{type(e).__name__}", category="llm", ok=False)
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
    stream: bool | None = None,
    _retry: bool = True,
) -> ChatResult:
    payload: dict[str, Any] = {
        "model": model or settings.chat_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": _effective_max_tokens(max_tokens, thinking),
        **_thinking_payload(thinking),
    }
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if stream is None:
        stream = not tools  # 无工具调用时默认流式，防网关超时；工具调用保持非流式（各家流式工具增量格式不一）
    data = _post_stream(payload) if stream else _post("/chat/completions", payload)
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    if _retry and not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
        # 思考把 token 吃光 / 模型空答：关思考、放大上限再试一次；仍为空就用思考内容兜底
        res2 = chat(messages, tools=tools, tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens * 2,
                    json_mode=json_mode, model=model, thinking=False, stream=stream, _retry=False)
        if res2.content.strip() or res2.tool_calls:
            return res2
        # 注意：不要把 reasoning_content 当答案返回——那是模型的内部草稿（"Hmm, let me think..."），
        # 直接显示给用户就是一坨思考过程。保留在 raw 里供排查，content 仍为空，交给上层 chat_answer 换模型。
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


def other_profile() -> str | None:
    """另一个配好的对话模型（siliconflow ↔ zju），没有就 None。"""
    cur = settings.llm_profile
    other = "zju" if cur == "siliconflow" else "siliconflow"
    key = settings.zju_api_key if other == "zju" else settings.sf_api_key
    return other if key else None


ANSWER_MAX_TOKENS = 6000          # 最终作答：Top10 表格 + 采购清单，3000 根本不够


def continue_if_truncated(res: "ChatResult", messages: list[dict[str, Any]], *, rounds: int = 3, **kw: Any) -> "ChatResult":
    """模型是被 max_tokens 掐断的（finish_reason=length）：接着写完，别让用户看半张表。"""
    kw = {k: v for k, v in kw.items() if k not in ("tools", "tool_choice")}
    out = res
    for _ in range(rounds):
        if (out.finish_reason or "") != "length" or not out.content.strip():
            break
        msgs = list(messages) + [
            {"role": "assistant", "content": out.content[-4000:]},
            {"role": "system", "content": "上面这段回答被长度限制截断了。请紧接着最后一个字继续写完，"
                                          "不要重复已经写过的内容、不要重新开头、不要写“继续”之类的过渡语。"},
        ]
        try:
            nxt = chat(msgs, **kw)
        except LLMError:
            break
        if not nxt.content.strip():
            break
        out = ChatResult(content=out.content + nxt.content, tool_calls=None, usage=nxt.usage,
                         finish_reason=nxt.finish_reason, raw=nxt.raw)
    return out


def chat_answer(messages: list[dict[str, Any]], **kw: Any) -> ChatResult:
    """要一个“有内容的回答”：当前模型空手而回时换一种问法，再不行换另一个模型。

    ZJU（vLLM）偶尔会在工具调用之后返回空 content，直接显示给用户就是“没有输出”。
    """
    kw.setdefault("max_tokens", ANSWER_MAX_TOKENS)
    res = chat(messages, **kw)
    if res.content.strip():
        return continue_if_truncated(res, messages, **kw)
    nudge = list(messages) + [{"role": "system", "content": "上一次没有输出内容。现在不要调用工具，直接用中文给出完整结论。"}]
    kw2 = {k: v for k, v in kw.items() if k not in ("tools", "tool_choice")}
    kw2["thinking"] = False
    res = chat(nudge, **kw2)
    if res.content.strip():
        return continue_if_truncated(res, nudge, **kw2)
    other = other_profile()
    if other:
        cur = settings.llm_profile
        try:
            settings.llm_profile = other
            res2 = chat(nudge, **kw2)
        except Exception as e:  # noqa: BLE001
            res2 = ChatResult(content=f"（备用模型 {other} 也失败：{str(e)[:120]}）")
        finally:
            settings.llm_profile = cur
        if res2.content.strip():
            res2.content += f"\n\n（本条由备用模型 {other} 作答：当前模型连续返回空内容）"
            return res2
    return ChatResult(content="（模型连续返回空内容：已换问法与备用模型重试仍无输出。可在「设置」页切换对话模型，"
                              "或把问题拆小一点再问一次。）")


def chat_stream(messages: list[dict[str, Any]], *, temperature: float = 0.3, max_tokens: int = 4096,
                model: str | None = None, thinking: bool | int | None = None) -> Iterator[str]:
    """流式输出纯文本（不含工具调用），供界面逐字显示。"""
    url = _base_url("chat") + "/chat/completions"
    payload = {"model": model or settings.chat_model, "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "stream": True, **_thinking_payload(thinking)}
    with activity.track("llm", payload["model"]), net.client(url, timeout=180.0) as client:
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
    try:
        return parse_json_loose(res.content)
    except LLMError:
        nudge = messages + [{"role": "user", "content": "上一次回复不是合法 JSON。只输出一个 JSON 对象本身，不要解释、不要代码块。"}]
        res = chat(nudge, temperature=0, max_tokens=max_tokens * 2, json_mode=True, thinking=False)
        return parse_json_loose(res.content)


def _salvage_truncated(text: str) -> Any:
    """输出被 max_tokens 截断的列表型 JSON（如 {"offers": [ {...}, {...}, {"a": ）：丢掉最后一个不完整对象，补上 ]}。"""
    starts = [p for p in (text.find("{"), text.find("[")) if p != -1]
    if not starts:
        return None
    body = text[min(starts):]
    while True:
        k = body.rfind("}")
        if k <= 0:
            return None
        head = body[:k + 1]
        for tail in ("]}", "}", "]", "]}}", "}}"):
            try:
                out = json.loads(head + tail)
                return out
            except json.JSONDecodeError:
                continue
        body = body[:k]  # 再往前找一个完整对象


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
    salvaged = _salvage_truncated(text)
    if salvaged is not None:
        return salvaged
    raise LLMError("模型返回了空内容（多为思考占满 max_tokens 或服务超时）" if not text else f"模型未返回可解析的 JSON：{text[:200]}")


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
