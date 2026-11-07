"""可复用的会话聊天组件：历史回放（每轮的处理步骤收起来）+ 实时处理状态 + 出处与下载。"""
from __future__ import annotations

import streamlit as st

from pathlib import Path

from . import answer_doc, llm, ui
from .agents import runner
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


TOOL_LABELS = {
    "aba_optimize": "计算分档最低成本方案", "aba_evaluate": "评估配方", "aba_prices": "查价格（当前价 / 期货）",
    "aba_experiment": "实验方案与校准", "aba_coefficients": "查看性能系数", "start_research": "启动后台调研",
    "refresh_market": "启动行情更新", "background_status": "查看后台进度", "register_material": "登记材料",
    "add_price": "登记价格", "undo_price": "撤销价格", "list_materials": "查看原料库", "get_material": "查看材料",
    "supplier_quotes": "查供应商报价", "find_suppliers": "找供应商", "import_trader_quotes": "导入贸易商日报",
    "daily_picks": "看日报最低价", "price_outlook": "看价格预判", "price_report": "看价格变动", "procurement_plan": "生成采购清单",
    "find_material_suppliers": "网查原料报价", "discover_materials": "上网找新材料", "list_formulations": "查看配方库",
    "kb_search": "检索知识库", "web_search": "网页搜索", "literature_search": "检索文献", "patent_search": "检索专利",
    "fetch_url": "读取网页",
}


def _label(name: str | None) -> str:
    return TOOL_LABELS.get(name or "", name or "处理")


def _turns(history: list[dict]) -> list[tuple[dict, list[dict], dict | None]]:
    """按轮分组：(用户消息, 这一轮的工具调用与结果, 最终回答)。"""
    out, cur = [], None
    for m in history:
        if m["role"] == "user":
            if cur:
                out.append(cur)
            cur = [m, [], None]
        elif cur is None:
            continue
        elif m["role"] == "tool" or (m["role"] == "assistant" and m.get("tool_calls")):
            cur[1].append(m)
        elif m["role"] == "assistant":
            cur[2] = m
    if cur:
        out.append(cur)
    return [tuple(t) for t in out]


def _steps(msgs: list[dict]) -> None:
    """一轮里调过的工具：收起来，展开能看每一步的结果（给想深究的人）。"""
    calls = [tc for m in msgs if m["role"] == "assistant" for tc in (m.get("tool_calls") or [])]
    results = {m.get("tool_call_id"): m for m in msgs if m["role"] == "tool"}
    if not calls:
        return
    names = "、".join(dict.fromkeys(_label(tc["name"]) for tc in calls))
    with st.expander(f"处理过程 · {len(calls)} 步：{names}", icon=":material/checklist:"):
        for tc in calls:
            r = results.get(tc["id"])
            st.markdown(f"**{_label(tc['name'])}**")
            if r and r.get("content"):
                st.text((r.get("content") or "")[:2500])


def _answer(content: str, citations: list[dict] | None, key: str) -> None:
    st.markdown(content)
    render_downloads(content, key)
    if citations:
        with st.expander("出处", icon=":material/menu_book:"):
            for c in citations:
                st.markdown(f"- **[{c['label']}]** {c['cite']}")


def _live_run(fn, key: str) -> None:
    """跑一轮：过程实时显示在一个可收起的状态框里，跑完收起，只留回答。"""
    with st.chat_message("assistant", avatar=":material/science:"):
        box = st.status("正在处理…", expanded=False)
        n = {"steps": 0}

        def on_event(kind, payload):
            if kind == "tool_call":
                n["steps"] += 1
                box.update(label=f"正在{_label(payload['name'])}…")
                with box:
                    st.markdown(f"**{_label(payload['name'])}**")
            elif kind == "tool_result":
                with box:
                    st.text(payload["text"][:1500])

        try:
            res = fn(on_event)
            box.update(label=f"处理完成 · {n['steps']} 步", state="complete")
            _answer(res["content"], res.get("citations"), key)
        except llm.LLMError as e:
            box.update(label="出错了", state="error")
            st.error(str(e))


def render_session_chat(session_id: int, placeholder: str, *, key: str = "chat", examples: list[str] | None = None) -> None:
    sess = runner.get_session(session_id)
    if not sess:
        st.info("会话不存在，请新建。")
        return
    history = runner.messages(session_id)
    pending_prompt = st.session_state.pop(f"{key}_example", None)
    if not history and examples and not pending_prompt:
        st.caption("试试这样问：")
        picked = st.pills("示例问法", examples, key=f"{key}_pills", label_visibility="collapsed")
        if picked:
            st.session_state[f"{key}_example"] = picked
            st.rerun()
    for user_msg, steps, final in _turns(history):
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(user_msg["content"])
        if steps or final:
            with st.chat_message("assistant", avatar=":material/science:"):
                _steps(steps)
                if final and final.get("content"):
                    _answer(final["content"], final.get("citations"), f"{key}_{final['id']}")

    if runner.is_pending(session_id):
        st.warning("上一轮没跑完就中断了（多半是服务重启或网络超时），已拿到的结果还在，回答还没生成。")
        if st.button("继续生成回答", type="primary", key=f"resume_{key}", icon=":material/play_arrow:"):
            _live_run(lambda cb: runner.resume(session_id, on_event=cb), f"{key}_resume")
            st.rerun()

    prompt = ui.chat_input(placeholder) or pending_prompt
    if prompt:
        if not settings.llm_ready:
            st.error("对话模型还没配置好，请到「设置 › 对话模型」填写。")
            st.stop()
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(prompt)
        _live_run(lambda cb: runner.chat(session_id, prompt, on_event=cb), f"{key}_new")
        st.rerun()


ADVISOR_EXAMPLES = [
    "性能不变或者提升，最低成本是多少？",
    "拉伸不降，透明度无所谓，一般、好、很好三档各给我最便宜的。",
    "现在的价格是多少？按期货算方案会便宜还是贵？",
    "有没有最新的材料，能提升拉伸又降成本？",
    "帮我跑一整轮：更新价格、找新料、出 50 组试验方案（Word）。",
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


# ---------------- 历史对话 ----------------

_NOWRAP = {"wrap": False} if ui._has_param(st.container, "wrap") else {}   # 标题和删除钮放一行，不折行

def _day_group(ts: str | None) -> str:
    from datetime import date, datetime

    try:
        days = (date.today() - datetime.fromisoformat(ts or "").date()).days
    except ValueError:
        return "更早"
    return "今天" if days <= 0 else "昨天" if days == 1 else "近 7 天" if days < 7 else "近 30 天" if days < 31 else "更早"


@st.dialog("删除这个对话？")
def _confirm_delete(sid: int, label: str, state_key: str) -> None:
    st.markdown(f"**{label}**")
    st.caption("这个对话的问答记录会一起删掉，不能恢复。")
    c1, c2 = st.columns(2)
    if c1.button("删除", type="primary", key="chat_del_ok", icon=":material/delete:", **ui._WIDE_BTN):
        runner.delete_session(sid)
        if st.session_state.get(state_key) == sid:
            st.session_state.pop(state_key, None)          # 删的是正在看的：回到最近的一个
        st.toast("已删除", icon=":material/check:")
        st.rerun()
    if c2.button("取消", key="chat_del_cancel", **ui._WIDE_BTN):
        st.rerun()


def render_history(role_key: str, state_key: str, current: int) -> None:
    """历史对话：新对话 + 按时间分组的列表，点一下打开，垃圾桶删除（先确认）。"""
    if st.button("新对话", key=f"{state_key}_new", icon=":material/add_comment:", type="primary", **ui._WIDE_BTN):
        st.session_state[state_key] = runner.new_chat(role_key, current)
        st.rerun()
    chats = runner.list_chats(role_key)
    if not chats:
        st.caption("还没有历史对话。问过的问题会留在这里，随时点开接着问。")
        return
    if len(chats) > 8:
        q = st.text_input("搜索历史对话", key=f"{state_key}_search", placeholder="搜索历史对话",
                          label_visibility="collapsed")
        if q.strip():
            chats = [c for c in chats if q.strip().lower() in (c["label"] + " " + (c["first_q"] or "")).lower()]
            if not chats:
                st.caption("没找到。")
    kw = {"height": 440} if len(chats) > 9 else {}
    with st.container(key="chat_hist", border=False, **kw):
        last = None
        for c in chats:
            g = _day_group(c["updated_at"])
            if g != last:
                st.caption(g)
                last = g
            is_cur = c["id"] == current
            when = (c["updated_at"] or "")[5:16].replace("T", " ")
            row = (st.container(horizontal=True, gap="small", vertical_alignment="center", **_NOWRAP)
                   if ui.HAS_HORIZONTAL else None)
            left, right = (row, row) if row is not None else st.columns([6, 1])
            with left:
                if st.button(c["label"], key=f"{state_key}_open_{c['id']}", type="secondary" if is_cur else "tertiary",
                             icon=":material/chat:" if is_cur else None, help=f"{when} · 问了 {c['n_q']} 次",
                             **ui._WIDE_BTN) and not is_cur:
                    st.session_state[state_key] = c["id"]
                    st.rerun()
            with right:
                if st.button(":material/delete:", key=f"{state_key}_del_{c['id']}", type="tertiary", help="删除这个对话"):
                    _confirm_delete(c["id"], c["label"], state_key)
