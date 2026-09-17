"""可复用的会话聊天组件（智能体页与对话页共用）：历史回放 + 工具调用时间线 + 出处。"""
from __future__ import annotations

import streamlit as st

from . import llm, ui
from .agents import runner
from .agents.roles import ROLES
from .config import settings


def render_session_chat(session_id: int, placeholder: str, *, key: str = "chat", examples: list[str] | None = None) -> None:
    sess = runner.get_session(session_id)
    if not sess:
        st.info("会话不存在，请新建。")
        return
    role = ROLES.get(sess["role_key"]) or ROLES["temp"]
    st.caption(role.name)
    if sess.get("summary"):
        with ui.expander("当前状态摘要"):
            st.write(sess["summary"])
    history = runner.messages(session_id)
    if not history and examples:
        with ui.expander("示例问法"):
            for ex in examples:
                st.code(ex, language="text")
    for m in history:
        if m["role"] == "user":
            with st.chat_message("user", avatar=":material/person:"):
                st.write(m["content"])
        elif m["role"] == "assistant":
            with st.chat_message("assistant", avatar=":material/science:"):
                for tc in m.get("tool_calls") or []:
                    with ui.expander(f"调用工具 {tc['name']}", step=True):
                        st.json(tc["arguments"])
                if m.get("content"):
                    st.write(m["content"])
                if m.get("citations"):
                    with ui.expander("出处"):
                        for c in m["citations"]:
                            st.markdown(f"- **[{c['label']}]** {c['cite']}")
        elif m["role"] == "tool":
            with st.chat_message("assistant", avatar=":material/build:"):
                with ui.expander(f"工具结果 {m.get('name')}", step=True):
                    st.text((m.get("content") or "")[:4000])

    if prompt := ui.chat_input(placeholder):
        if not settings.llm_ready:
            st.error("对话模型未配置，请到「设置」页填写密钥。")
            st.stop()
        with st.chat_message("user", avatar=":material/person:"):
            st.write(prompt)
        with st.chat_message("assistant", avatar=":material/science:"):
            holder = st.container()

            def on_event(kind, payload):
                if kind == "tool_call":
                    with holder:
                        with ui.status(f"调用 {payload['name']}", step=True):
                            st.json(payload["arguments"])
                elif kind == "tool_result":
                    with holder:
                        with ui.status(f"结果 {payload['name']}", step=True, state="complete"):
                            st.text(payload["text"][:3000])

            try:
                res = runner.chat(session_id, prompt, on_event=on_event)
                st.write(res["content"])
                if res["citations"]:
                    with ui.expander("出处"):
                        for c in res["citations"]:
                            st.markdown(f"- **[{c['label']}]** {c['cite']}")
            except llm.LLMError as e:
                st.error(str(e))
        st.rerun()


ADVISOR_EXAMPLES = [
    "我们现在用的 LLDPE 是 12000 元/吨。供应商 A 报了一种国产 7042（密度 0.918，熔点 122℃，MFR 2.0，丁烯共聚），到厂价 8300 元/吨，2026-09-16 报价。膜是单层膜，50 微米，请出降本方案。",
    "有什么料可以替代现用 LLDPE？按物性窗口筛一下。",
    "7042 涨到 9200 了，重新出一下方案，并说明和涨价前的差别。",
    "第 1 名方案为什么热封是 ↓？依据是什么？",
]


def get_or_create_session(role_key: str, state_key: str) -> int:
    sid = st.session_state.get(state_key)
    if sid and runner.get_session(sid):
        return sid
    for s in runner.list_sessions():
        if s["role_key"] == role_key:
            st.session_state[state_key] = s["id"]
            return s["id"]
    sid = runner.create_session(role_key)
    st.session_state[state_key] = sid
    return sid
