from __future__ import annotations

import streamlit as st

from polysage import chat_widget
from polysage.agents import runner
from polysage.agents.roles import ROLES

from polysage import ui

ui.page_header("对话", "配方推荐助手与六个长期角色；每个角色固定分工与工具。")

with st.sidebar:
    st.markdown("##### 会话")
    role_key = st.selectbox("新建会话的角色", list(ROLES), index=0, format_func=lambda k: ROLES[k].name)
    if st.button("新建会话"):
        st.session_state["session_id"] = runner.create_session(role_key)
        st.rerun()
    sessions = runner.list_sessions()
    if sessions:
        ids = [s["id"] for s in sessions]
        cur = st.session_state.get("session_id")
        idx = ids.index(cur) if cur in ids else 0
        pick = st.radio("选择会话", ids, index=idx, format_func=lambda i: next(f"#{s['id']} {s['title']}" for s in sessions if s["id"] == i),
                        label_visibility="collapsed")
        st.session_state["session_id"] = pick
        b1, b2 = st.columns(2)
        if b1.button("归档并续开", help="生成状态摘要，归档旧会话并新建同角色会话（带摘要）"):
            nid = runner.archive_session(pick)
            st.session_state["session_id"] = nid
            st.rerun()
        if b2.button("删除", type="secondary"):
            runner.delete_session(pick)
            st.session_state.pop("session_id", None)
            st.rerun()

sid = st.session_state.get("session_id")
if not sid or not runner.get_session(sid):
    st.caption("在左侧新建会话。角色决定系统提示与可用工具。")
    st.stop()

chat_widget.render_session_chat(sid, "向该角色提问（可要求它检索、入库、算成本、生成配方…）", key="chat_page",
                                examples=chat_widget.ADVISOR_EXAMPLES if runner.get_session(sid)["role_key"] == "advisor" else None)
