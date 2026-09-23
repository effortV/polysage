"""会话管理 + 函数调用循环。

- 会话与消息持久化在 SQLite；每个会话绑定一个角色。
- 一轮用户消息：system(角色+固定上下文) + 历史 + 用户 → 模型；若返回 tool_calls 则执行并回填，最多 max_steps 轮。
- 引用：工具返回的 citations 汇总到最终 assistant 消息的 citations_json。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .. import activity, db, kb, llm
from . import tools as T
from .roles import ROLES, system_prompt

MAX_HISTORY_CHARS = 40000
# 每轮对话里同一工具的调用上限（防止模型在检索上打转）
TOOL_CALL_CAPS = {"web_search": 3, "literature_search": 2, "patent_search": 2, "fetch_url": 3, "kb_search": 4,
                  "recommend_schemes": 2, "compute_cost": 6, "list_materials": 2, "list_formulations": 2,
                  "price_report": 2, "daily_picks": 2, "price_outlook": 2, "procurement_plan": 3,
                  "find_material_suppliers": 2, "find_suppliers": 2, "supplier_quotes": 3, "get_base": 2,
                  "screen_materials": 2, "explain_scheme": 4}


# ---------- 会话 ----------

def create_session(role_key: str, title: str | None = None) -> int:
    role = ROLES.get(role_key) or ROLES["temp"]
    title = title or role.name
    return db.insert("sessions", {"role_key": role_key, "title": title, "created_at": db.now(), "updated_at": db.now()})


def list_sessions(include_archived: bool = False) -> list[dict[str, Any]]:
    if include_archived:
        return db.q("SELECT * FROM sessions ORDER BY archived, updated_at DESC")
    return db.q("SELECT * FROM sessions WHERE archived=0 ORDER BY updated_at DESC")


def get_session(session_id: int) -> dict[str, Any] | None:
    return db.q1("SELECT * FROM sessions WHERE id=?", (session_id,))


def rename_session(session_id: int, title: str) -> None:
    db.update("sessions", session_id, {"title": title, "updated_at": db.now()})


def delete_session(session_id: int) -> None:
    db.delete("sessions", session_id)


def messages(session_id: int) -> list[dict[str, Any]]:
    rows = db.q("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))
    for r in rows:
        r["tool_calls"] = db.loads(r.get("tool_calls_json"), None)
        r["citations"] = db.loads(r.get("citations_json"), [])
    return rows


def _add(session_id: int, role: str, content: str | None, **extra: Any) -> int:
    data = {"session_id": session_id, "role": role, "content": content, "created_at": db.now()}
    if extra.get("tool_calls") is not None:
        data["tool_calls_json"] = db.dumps(extra["tool_calls"])
    if extra.get("tool_call_id"):
        data["tool_call_id"] = extra["tool_call_id"]
    if extra.get("name"):
        data["name"] = extra["name"]
    if extra.get("citations"):
        data["citations_json"] = db.dumps(extra["citations"])
    mid = db.insert("messages", data)
    db.update("sessions", session_id, {"updated_at": db.now()})
    return mid


MAX_TOOL_CHARS = 6000


def _clip_tool(text: str) -> str:
    """工具结果太长会把模型的回答挤没：超长时留头留尾，中间标明省略。"""
    text = text or ""
    if len(text) <= MAX_TOOL_CHARS:
        return text
    head, tail = text[: MAX_TOOL_CHARS - 1200], text[-1000:]
    return f"{head}\n…（结果过长，中间省略 {len(text) - MAX_TOOL_CHARS + 200} 字；完整内容见对应页面）…\n{tail}"


def _to_api(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """数据库消息 → OpenAI 格式；超长时从最早的开始丢弃（保留最近的）。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["role"] == "assistant":
            m: dict[str, Any] = {"role": "assistant", "content": r.get("content") or ""}
            if r.get("tool_calls"):
                m["tool_calls"] = [{"id": tc["id"], "type": "function",
                                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                                   for tc in r["tool_calls"]]
            out.append(m)
        elif r["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": r.get("tool_call_id"), "content": r.get("content") or ""})
        else:
            out.append({"role": r["role"], "content": r.get("content") or ""})
    total = sum(len(m.get("content") or "") for m in out)
    while out and total > MAX_HISTORY_CHARS:
        m = out.pop(0)
        total -= len(m.get("content") or "")
        # 不能让 tool 消息孤立在开头
        while out and out[0]["role"] == "tool":
            total -= len(out[0].get("content") or "")
            out.pop(0)
    return out


# ---------- 对话 ----------

def chat(session_id: int, user_text: str, *, max_steps: int = 8,
         on_event: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """执行一轮对话（含工具调用）。on_event(kind, payload) 用于界面实时显示：tool_call / tool_result / final。"""
    sess = get_session(session_id)
    if not sess:
        raise ValueError("会话不存在")
    role = ROLES.get(sess["role_key"]) or ROLES["temp"]
    _add(session_id, "user", user_text)
    sys_msg = {"role": "system", "content": system_prompt(sess["role_key"], sess.get("summary") or "")}
    citations: list[dict[str, Any]] = []
    steps = 0
    calls: dict[str, int] = {}
    while True:
        history = _to_api(messages(session_id))
        res = llm.chat([sys_msg, *history], tools=T.schemas(role.tools), tool_choice="auto", temperature=0.3, max_tokens=4096, thinking=False)
        if res.tool_calls and steps < max_steps:
            _add(session_id, "assistant", res.content or "", tool_calls=res.tool_calls)
            for tc in res.tool_calls:
                if on_event:
                    on_event("tool_call", tc)
                activity.note(f"对话调用工具 {tc['name']}", category="tool")
                calls[tc["name"]] = calls.get(tc["name"], 0) + 1
                cap = TOOL_CALL_CAPS.get(tc["name"])
                if cap and calls[tc["name"]] > cap:
                    text, cits = (f"本轮 {tc['name']} 已调用 {cap} 次，不再检索。请基于已有结果直接回答；"
                                  "证据不足的地方明确写“知识库暂无依据，属经验判断，建议先跑流水线 ①③ 入库后再问”。"), []
                else:
                    text, cits = T.run(tc["name"], tc["arguments"])
                citations.extend(c for c in cits if c["source_id"] not in {x["source_id"] for x in citations})
                _add(session_id, "tool", _clip_tool(text), tool_call_id=tc["id"], name=tc["name"])
                if on_event:
                    on_event("tool_result", {"name": tc["name"], "text": text})
            steps += 1
            continue
        if res.tool_calls and steps >= max_steps:
            # 到达步数上限：不再执行工具，强制模型基于已有信息作答
            _add(session_id, "assistant", res.content or "", tool_calls=res.tool_calls)
            for tc in res.tool_calls:
                _add(session_id, "tool", "（已达到本轮工具调用上限，未执行）请基于已有信息直接回答。", tool_call_id=tc["id"], name=tc["name"])
            history = _to_api(messages(session_id))
            res = llm.chat_answer([sys_msg, *history, {"role": "system", "content": "工具调用已达上限。现在不要再调用工具，直接给出结论；证据不足处写明“待验证/经验判断”。"}],
                                  temperature=0.3, max_tokens=3000, thinking=False)
        if not (res.content or "").strip():
            # 模型调完工具后空手而回：换问法/换模型再要一次结论
            res = llm.chat_answer([sys_msg, *history], temperature=0.3, max_tokens=4096, thinking=False)
        content = res.content or "（模型未返回内容）"
        _add(session_id, "assistant", content, citations=citations)
        if on_event:
            on_event("final", {"content": content, "citations": citations})
        return {"content": content, "citations": citations, "usage": res.usage, "steps": steps}


def summarize_session(session_id: int) -> str:
    """输出“当前状态摘要”（约束、已排除项、待办），存入会话与 00_项目/状态摘要.md。"""
    hist = [m for m in messages(session_id) if m["role"] in ("user", "assistant") and m.get("content")]
    text = "\n".join(f"{m['role']}: {m['content'][:1500]}" for m in hist[-30:])
    res = llm.chat([
        {"role": "system", "content": "你是项目记录员。把下面的对话整理成“当前状态摘要”，固定标题：已确认的约束与事实 / 已排除的方案与原因 / 待办与下一步 / 未决问题。只保留结论，标注依据编号，不超过 600 字。"},
        {"role": "user", "content": text or "（空对话）"},
    ], temperature=0.2, thinking=False)
    db.update("sessions", session_id, {"summary": res.content, "updated_at": db.now()})
    kb.write_text(kb.PROJECT_FILES["summary"], res.content + f"\n\n（来自会话 #{session_id}，{db.now()}）\n")
    return res.content


def archive_session(session_id: int, new_session: bool = True) -> int | None:
    """归档：先生成摘要，再新建同名同角色会话并带上摘要（内部技术路线 3.3）。"""
    sess = get_session(session_id)
    if not sess:
        return None
    summary = summarize_session(session_id)
    db.update("sessions", session_id, {"archived": 1, "updated_at": db.now()})
    if not new_session:
        return None
    nid = create_session(sess["role_key"], sess["title"])
    db.update("sessions", nid, {"summary": summary})
    return nid
