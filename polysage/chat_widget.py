"""可复用的会话聊天组件（智能体页与对话页共用）：历史回放 + 工具调用时间线 + 出处。"""
from __future__ import annotations

import streamlit as st

from pathlib import Path

from . import answer_doc, llm, ui
from .agents import runner
from .agents.roles import ROLES
from .config import settings



_MIME = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".xls": "application/vnd.ms-excel", ".csv": "text/csv",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".md": "text/markdown", ".json": "application/json", ".pdf": "application/pdf"}


def render_downloads(content: str, key: str) -> None:
    """回答下面的下载条：它生成的表格文件 + 本条回答导出的 Word。"""
    if not (content or "").strip():
        return
    files = answer_doc.referenced_files(content)
    cols = st.columns(min(len(files) + 1, 4))
    for i, f in enumerate(files):
        try:
            data = f.read_bytes()
        except OSError:
            continue
        cols[i % len(cols)].download_button(f"下载 {f.name[:28]}", data, file_name=f.name,
                                            mime=_MIME.get(f.suffix.lower(), "application/octet-stream"),
                                            key=f"dl_{key}_{i}")
    col = cols[len(files) % len(cols)]
    if col.button("导出本条回答 Word", key=f"docx_{key}"):
        try:
            path = answer_doc.answer_to_docx(content)
            st.session_state[f"docx_path_{key}"] = str(path)
        except Exception as e:  # noqa: BLE001
            st.error(f"导出失败：{e}")
    saved = st.session_state.get(f"docx_path_{key}")
    if saved and Path(saved).exists():
        st.download_button("下载 Word", Path(saved).read_bytes(), file_name=Path(saved).name,
                           mime=_MIME[".docx"], key=f"dldocx_{key}")


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
                    render_downloads(m["content"], f"{key}_{m['id']}")
                if m.get("citations"):
                    with ui.expander("出处"):
                        for c in m["citations"]:
                            st.markdown(f"- **[{c['label']}]** {c['cite']}")
        elif m["role"] == "tool":
            with st.chat_message("assistant", avatar=":material/build:"):
                with ui.expander(f"工具结果 {m.get('name')}", step=True):
                    st.text((m.get("content") or "")[:4000])

    if runner.is_pending(session_id):
        st.warning("上一轮没跑完就中断了（多半是服务重启或网络超时），工具结果已经拿到，回答还没生成。")
        if st.button("继续生成回答", type="primary", key=f"resume_{key}"):
            with st.chat_message("assistant", avatar=":material/science:"):
                holder2 = st.container()

                def on_event2(kind, payload):
                    if kind == "tool_call":
                        with holder2:
                            with ui.status(f"调用 {payload['name']}", step=True):
                                st.json(payload["arguments"])
                    elif kind == "tool_result":
                        with holder2:
                            with ui.status(f"结果 {payload['name']}", step=True, state="complete"):
                                st.text(payload["text"][:3000])

                try:
                    res = runner.resume(session_id, on_event=on_event2)
                    st.write(res["content"])
                    render_downloads(res["content"], f"{key}_resume")
                except llm.LLMError as e:
                    st.error(str(e))
            st.rerun()

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
                render_downloads(res["content"], f"{key}_new")
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
