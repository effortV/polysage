from __future__ import annotations

import streamlit as st

from polysage import auth, llm, providers, ui
from polysage.config import settings
from polysage.envfile import set_env
from polysage.pipeline import task
from polysage.sources import elsevier

ui.page_header("设置", "对话模型随便换（填地址、密钥、模型即可）、检索接口、访问密码、任务书。")

with st.container(horizontal=True):
    for name, ok in settings.status().items():
        st.badge(name, icon=":material/check:" if ok else ":material/remove:", color="green" if ok else "gray")

tab_llm, tab_data, tab_pw, tab_task = st.tabs(["对话模型", "检索与数据源", "访问密码", "任务书"],
                                              key="settings-tab", on_change="rerun")


def _ping(pid: str) -> tuple[bool, str]:
    """临时切到这家试一句，再切回来。"""
    cur = settings.llm_profile
    try:
        settings.llm_profile = pid
        return llm.ping()
    finally:
        settings.llm_profile = cur


def _provider_form(p: dict | None, key: str) -> None:
    """新增或编辑一家服务商。新增时可以从常用的里选一个，自动填好地址和模型。"""
    preset = {}
    if p is None:
        names = [x["name"] for x in providers.PRESETS]
        pick = st.selectbox("从常用的选（会自动填好地址和模型）", names, key=f"{key}_preset")
        preset = next(x for x in providers.PRESETS if x["name"] == pick)
    base = p or preset
    with st.form(f"{key}_form", border=False):
        c1, c2 = st.columns(2)
        name = c1.text_input("名称", base.get("name", ""), placeholder="比如：硅基流动、公司内网模型")
        model = c2.text_input("模型", base.get("model", ""), placeholder="比如：deepseek-ai/DeepSeek-V4-Pro")
        url = st.text_input("接口地址（OpenAI 兼容，一般以 /v1 结尾）", base.get("base_url", ""),
                            placeholder="https://…/v1")
        api_key = st.text_input("密钥", type="password",
                                placeholder=("已填 " + providers.masked(p.get("api_key", "")) + "，留空不改") if p else "sk-…")
        styles = list(providers.THINKING_STYLES)
        thinking = st.selectbox("思考开关的写法", styles, index=styles.index(base.get("thinking", "none")),
                                format_func=providers.THINKING_STYLES.get)
        if st.form_submit_button("保存", type="primary", icon=":material/save:"):
            if not providers.slug_ok(url) or not model.strip():
                st.error("接口地址和模型都要填，地址以 http(s):// 开头。")
            elif p is None and not api_key.strip():
                st.error("新加的服务商要填密钥。")
            else:
                pid = providers.upsert({"id": (p or {}).get("id"), "name": name, "base_url": url, "model": model,
                                        "api_key": api_key, "thinking": thinking})
                st.toast(f"已保存：{name or pid}", icon=":material/check:")
                st.rerun()


def _render_llm() -> None:
    data = providers.load()
    plist = data["providers"]
    names = {p["id"]: f"{p['name']} · {p['model']}" for p in plist}
    cur = settings.llm_profile if settings.llm_profile in names else data.get("active")
    st.markdown("**当前使用**")
    pick = st.segmented_control("当前使用", list(names), default=cur, format_func=names.get, key="llm_pick",
                                label_visibility="collapsed")
    if pick and pick != cur:
        p = providers.get(pick)
        if not providers.ready(p):
            st.error(f"{p['name']} 还没填全（地址、密钥、模型），先在下面编辑。")
        else:
            providers.set_active(pick)
            st.toast(f"已切换到 {p['name']}", icon=":material/swap_horiz:")
            st.rerun()
    fb = providers.get(providers.fallback(cur))
    st.caption(f"当前模型连续答不出来时，自动换备用：{fb['name']} · {fb['model']}" if fb else "只配了一家，没有备用。")

    for p in plist:
        with st.container(border=True):
            head, act = st.columns([3, 2], vertical_alignment="center")
            with head:
                st.markdown(f"**{p['name']}**　" + (":blue-badge[当前使用]" if p["id"] == cur else "")
                            + ("" if providers.ready(p) else "　:orange-badge[未填全]"))
                st.caption(f"{p['base_url'] or '（未填地址）'}　·　模型 {p['model'] or '—'}　·　密钥 {providers.masked(p.get('api_key', ''))}")
            with act:
                with st.container(horizontal=True, horizontal_alignment="right"):
                    if st.button("测试", key=f"ping_{p['id']}", icon=":material/bolt:", disabled=not providers.ready(p)):
                        ok, msg = _ping(p["id"])
                        st.session_state[f"ping_msg_{p['id']}"] = (ok, msg)
                    if p["id"] != cur and st.button("删除", key=f"del_{p['id']}", icon=":material/delete:"):
                        providers.delete(p["id"])
                        st.rerun()
            res = st.session_state.get(f"ping_msg_{p['id']}")
            if res:
                (st.success if res[0] else st.error)(res[1])
            with st.expander("编辑", icon=":material/edit:"):
                _provider_form(p, f"edit_{p['id']}")

    with st.expander("添加服务商", icon=":material/add:"):
        _provider_form(None, "new")


def _render_data() -> None:
    st.caption("留空的项不改。保存后立即生效。")
    with st.form("data_keys"):
        st.markdown("**网页搜索**（找新料、查价、找供应商都靠它；服务器 IP 被免费搜索限流，需要这把钥匙）")
        bocha = st.text_input("博查搜索 API Key", type="password",
                              placeholder=("已填 " + providers.masked(settings.bocha_api_key)) if settings.bocha_api_key else "未填")
        st.markdown("**向量检索**（知识库语义搜索，固定用硅基流动 bge-m3）")
        sf = st.text_input("硅基流动 API Key", type="password",
                           placeholder=("已填 " + providers.masked(settings.sf_api_key)) if settings.sf_api_key else "未填")
        st.markdown("**文献数据库**（Scopus / ScienceDirect，可选）")
        c1, c2 = st.columns(2)
        els = c1.text_input("Elsevier API Key", type="password",
                            placeholder=("已填 " + providers.masked(settings.elsevier_api_key)) if settings.elsevier_api_key else "未填")
        inst = c2.text_input("Elsevier Insttoken（可选）", type="password", placeholder="留空不改")
        mail = st.text_input("Unpaywall / OpenAlex 联系邮箱（可选）", settings.unpaywall_email)
        if st.form_submit_button("保存", type="primary", icon=":material/save:"):
            set_env({"BOCHA_API_KEY": bocha or None, "SILICONFLOW_API_KEY": sf or None, "ELSEVIER_API_KEY": els or None,
                     "ELSEVIER_INSTTOKEN": inst or None, "UNPAYWALL_EMAIL": mail or None, "OPENALEX_MAILTO": mail or None})
            st.toast("已保存", icon=":material/check:")
            st.rerun()
    if st.button("测试 Scopus", icon=":material/bolt:", disabled=not settings.elsevier_api_key):
        ok, msg = elsevier.ping()
        (st.success if ok else st.error)(msg)


def _render_pw() -> None:
    if auth.password_required():
        st.caption("现在打开网页需要输入访问密码。" + ("（已加密保存）" if settings.app_password_hash else "（旧版明文保存，改一次就会换成加密保存）"))
    else:
        st.caption("现在打开网页不需要密码。对外开放前请设置。")
    if not auth.admin_key_set():
        st.warning("服务器还没有设置核心密钥，暂时不能在这里改密码。请联系管理员。")
        return
    with st.form("change_pw"):
        st.markdown("**修改访问密码**（需要核心密钥）")
        key = st.text_input("核心密钥", type="password")
        new = st.text_input("新密码（至少 6 位）", type="password")
        again = st.text_input("再输一次新密码", type="password")
        if st.form_submit_button("修改", type="primary", icon=":material/lock_reset:"):
            ok, msg = auth.change_password(key, new, again)
            (st.success if ok else st.error)(msg)


def _render_task() -> None:
    t = task.load()
    p = t["product"]
    with st.form("task_form"):
        st.caption("给智能体看的项目背景。改了之后，对话里的回答会按新的描述来。")
        p["name"] = st.text_input("产品", p.get("name", ""))
        p["layers"] = st.text_input("层结构", p.get("layers", ""))
        p["thickness"] = st.text_input("厚度", p.get("thickness", ""))
        t["formulation_note"] = st.text_area("现配方说明", t.get("formulation_note", ""), height=90)
        t["targets"]["note"] = st.text_area("目标", t["targets"].get("note", ""), height=90)
        if st.form_submit_button("保存", type="primary", icon=":material/save:"):
            task.save(t)
            st.toast("任务书已保存", icon=":material/check:")


for _tab, _fn in ((tab_llm, _render_llm), (tab_data, _render_data), (tab_pw, _render_pw), (tab_task, _render_task)):
    with _tab:
        if _tab.open:
            _fn()
