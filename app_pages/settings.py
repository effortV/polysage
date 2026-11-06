from __future__ import annotations

import streamlit as st

from polysage import auth, embedsig, jobs, llm, providers, ui
from polysage.config import settings
from polysage.envfile import set_env
from polysage.ingest import embed_missing, rebuild_vectors
from polysage.pipeline import task
from polysage.rag import index as rag_index
from polysage.sources import elsevier, openalex, semantic_scholar, wiley

jobs.JOB_NAMES.setdefault("embed_rebuild", "重建向量索引")
jobs.JOB_NAMES.setdefault("embed_fill", "补建向量")

ui.page_header("设置", "对话模型、向量检索都能随便换（填地址、密钥、模型即可）；还有搜索与文献接口、访问密码、任务书。")

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


def _embed_form(p: dict | None, key: str) -> None:
    """新增或编辑一家向量检索服务。新增时可以从常用的里选一个，自动填好地址和模型。"""
    preset = {}
    if p is None:
        names = [x["name"] for x in providers.EMBED_PRESETS]
        pick = st.selectbox("从常用的选（会自动填好地址和模型）", names, key=f"{key}_preset")
        preset = next(x for x in providers.EMBED_PRESETS if x["name"] == pick)
    base = p or preset
    with st.form(f"{key}_form", border=False):
        c1, c2 = st.columns(2)
        name = c1.text_input("名称", base.get("name", ""), placeholder="比如：硅基流动、公司内网向量服务")
        url = c2.text_input("接口地址（OpenAI 兼容，一般以 /v1 结尾）", base.get("base_url", ""), placeholder="https://…/v1")
        c1, c2 = st.columns(2)
        emb = c1.text_input("嵌入模型", base.get("embed_model", ""), placeholder="比如：BAAI/bge-m3")
        rr = c2.text_input("重排模型（可空）", base.get("rerank_model", ""), placeholder="比如：BAAI/bge-reranker-v2-m3",
                           help="把粗选出来的几十段再按和问题的相关度精排一遍。接口位置会自动找（/rerank 或百炼的 /compatible-api/v1/reranks）。没有就留空：按关键词 + 向量混合排序。")
        c1, c2 = st.columns([3, 1])
        ph = "sk-…"
        if p:
            k = providers.effective_key(p)
            ph = ("已填 " + providers.masked(k) + "，留空不改") if k else "未填"
        api_key = c1.text_input("密钥", type="password", placeholder=ph,
                                help="硅基流动的密钥和对话模型那边共用，填一边就行。")
        batch = c2.number_input("每次发几段", 1, 256, int(base.get("batch") or 16),
                                help="一次请求最多嵌入几段文字。填大了也没关系：服务报上限时会自动改小。")
        if st.form_submit_button("保存", type="primary", icon=":material/save:"):
            if not providers.slug_ok(url) or not emb.strip():
                st.error("接口地址和嵌入模型都要填，地址以 http(s):// 开头。")
            elif p is None and not api_key.strip() and "siliconflow" not in url:
                st.error("新加的服务要填密钥。")
            else:
                pid = providers.embed_upsert({"id": (p or {}).get("id"), "name": name, "base_url": url, "embed_model": emb,
                                              "rerank_model": rr, "api_key": api_key, "batch": batch})
                if p and p["id"] == providers.load().get("embed_active"):
                    rag_index.invalidate()
                st.toast(f"已保存：{name or pid}", icon=":material/check:")
                st.rerun()


def _start_vector_job(name: str) -> None:
    fn = rebuild_vectors if name == "embed_rebuild" else embed_missing
    if jobs.start(name, fn):
        st.toast(f"已在后台开始{jobs.JOB_NAMES[name]}", icon=":material/autorenew:")
    else:
        run = jobs.info() or {}
        st.warning(f"后台正在跑「{jobs.JOB_NAMES.get(run.get('name'), run.get('name'))}」，等它跑完再重建。")


def _index_status() -> None:
    s = embedsig.state()
    run = jobs.info()
    rebuilding = bool(run and run["name"] in ("embed_rebuild", "embed_fill"))
    res = jobs.result("embed_rebuild")
    fill = jobs.result("embed_fill")
    with st.container(border=True):
        st.markdown("**知识库向量索引**")
        if rebuilding:
            st.info(f"正在用 {settings.embed_model} {jobs.JOB_NAMES[run['name']]}：已完成 {s['embedded']} / {s['chunks']} 段"
                    f"（{run['elapsed'] / 60:.0f} 分钟）。这期间检索照常可用，刷新页面看进度。", icon=":material/autorenew:")
        elif s["chunks"] == 0:
            st.caption("知识库里还没有资料。以后入库的资料会自动用当前嵌入模型建向量。")
        elif not s["matches"]:
            st.warning(f"现有 {s['embedded']} 段向量是用 **{s['stored']}** 建的，和当前的 **{s['current']}** 对不上。"
                       "语义搜索已暂停（只按关键词搜），新入库的资料也先不建向量。点下面「重建向量索引」换成新模型。",
                       icon=":material/warning:")
        else:
            st.caption(f"{s['embedded']} / {s['chunks']} 段已建向量（{s['stored'] or s['current']}）。"
                       + ("" if s["embedded"] >= s["chunks"] else "有一部分还没建，点「补建缺的向量」。"))
        if fill and fill["done"] and not rebuilding:
            if fill["error"]:
                st.error(f"上次补建失败：{fill['error']}")
            elif fill["result"] is not None:
                st.success(f"上次补建完成：{fill['result']} 段")
        if res and res["done"] and not rebuilding:
            if res["error"]:
                st.error(f"上次重建失败：{res['error']}")
            elif res["result"]:
                r = res["result"]
                st.success(f"上次重建完成：{r['rebuilt']} 段，{r['model']}（{r['dim']} 维）"
                           + (f"，还剩 {r['left']} 段没建" if r["left"] else ""))
        with st.container(horizontal=True):
            busy = bool(run)
            if st.button("补建缺的向量", icon=":material/add_circle:", key="embed_fill",
                         disabled=busy or not s["ready"] or not s["matches"] or s["embedded"] >= s["chunks"],
                         help="给还没建向量的资料补上（后台跑，不用等）。"):
                _start_vector_job("embed_fill")
                st.rerun()
            if st.button("重建向量索引", icon=":material/autorenew:", key="embed_rebuild",
                         type="primary" if not s["matches"] else "secondary",
                         disabled=busy or not s["ready"] or s["chunks"] == 0,
                         help=f"清掉旧向量，用当前的 {settings.embed_model or '嵌入模型'} 全部重建。{s['chunks']} 段大约要 "
                              f"{max(1, s['chunks'] // 600)} 分钟，期间检索照常可用。"):
                _start_vector_job("embed_rebuild")
                st.rerun()


def _render_embed() -> None:
    data = providers.load()
    plist = data["embed_providers"]
    names = {p["id"]: f"{p['name']} · {p.get('embed_model') or '—'}" for p in plist}
    cur = data.get("embed_active") if data.get("embed_active") in names else plist[0]["id"]
    st.markdown("**向量检索**（知识库语义搜索：嵌入模型 + 重排模型，换哪家都行）")
    pick = st.segmented_control("当前使用", list(names), default=cur, format_func=names.get, key="embed_pick",
                                label_visibility="collapsed")
    if pick and pick != cur:
        p = providers.embed_get(pick)
        if not providers.embed_ready(p):
            st.error(f"{p['name']} 还没填全（地址、密钥、嵌入模型），先在下面编辑。")
        else:
            old_sig = embedsig.sig()
            providers.embed_set_active(pick)
            rag_index.invalidate()
            changed = embedsig.sig() != old_sig and embedsig.state()["embedded"] > 0
            st.toast(f"已切换到 {p['name']}" + ("，嵌入模型变了，记得重建向量索引" if changed else ""),
                     icon=":material/swap_horiz:")
            st.rerun()

    for p in plist:
        with st.container(border=True):
            head, act = st.columns([3, 2], vertical_alignment="center")
            with head:
                st.markdown(f"**{p['name']}**　" + (":blue-badge[当前使用]" if p["id"] == cur else "")
                            + ("" if providers.embed_ready(p) else "　:orange-badge[未填全]"))
                st.caption(f"{p['base_url'] or '（未填地址）'}　·　嵌入 {p.get('embed_model') or '—'}　·　"
                           f"重排 {p.get('rerank_model') or '不用'}　·　密钥 {providers.masked(providers.effective_key(p))}")
            with act:
                with st.container(horizontal=True, horizontal_alignment="right"):
                    if st.button("测试", key=f"eping_{p['id']}", icon=":material/bolt:", disabled=not providers.embed_ready(p)):
                        st.session_state[f"eping_msg_{p['id']}"] = llm.embed_ping(p)
                    if p["id"] != cur and st.button("删除", key=f"edel_{p['id']}", icon=":material/delete:"):
                        providers.embed_delete(p["id"])
                        st.rerun()
            res = st.session_state.get(f"eping_msg_{p['id']}")
            if res:
                (st.success if res[0] else st.error)(res[1])
            with st.expander("编辑", icon=":material/edit:"):
                _embed_form(p, f"eedit_{p['id']}")

    with st.expander("添加向量检索服务", icon=":material/add:"):
        _embed_form(None, "enew")
    _index_status()


def _secret(v: str) -> str | None:
    """留空 = 不改；填一个减号 = 清空。"""
    v = (v or "").strip()
    if not v:
        return None
    return "" if v == "-" else v


def _ph(value: str, hint: str = "") -> str:
    return (f"已填 {providers.masked(value)}，留空不改") if value else (hint or "未填")


def _openalex_keys_view() -> None:
    rows = openalex.key_report()
    if not rows:
        st.caption("还没填 OpenAlex key。不填也能用，但慢、容易被限流。")
        return
    ok = sum(1 for _, s in rows if s == "可用")
    unchecked = sum(1 for _, s in rows if s == "未检查")
    head = f"**共 {len(rows)} 个 key，可用 {ok} 个**" + (f"（{unchecked} 个还没检查）" if unchecked else "")
    st.markdown(head + " - " + " - ".join(f"`{k}` {s}" for k, s in rows))


def _render_data() -> None:
    _render_embed()
    st.divider()
    with st.form("data_keys"):
        st.markdown("**网页搜索**（找新料、查价、找供应商都靠它；服务器 IP 被免费搜索限流，需要这把钥匙）")
        bocha = st.text_input("博查搜索 API Key", type="password", placeholder=_ph(settings.bocha_api_key))
        st.markdown("**文献 API**（联网找文献 / 取全文；只存在服务器上，留空 = 不改，填一个减号 `-` = 清空）")
        left, right = st.columns([2, 3], vertical_alignment="center")
        oa = left.text_area("OpenAlex key", placeholder="OpenAlex key（一行一个；留空 = 不改）", height=100,
                            label_visibility="collapsed",
                            help="可以填多个：一个失效或今天额度用完，自动换下一个。填了会覆盖原来的整组。")
        with right:
            _openalex_keys_view()
        c1, c2, c3, c4 = st.columns(4)
        els = c1.text_input("Elsevier API key（Scopus 检索 + 校园 IP 下全文）", type="password",
                            placeholder=_ph(settings.elsevier_api_key))
        inst = c2.text_input("Elsevier Insttoken（可选）", type="password", placeholder=_ph(settings.elsevier_insttoken),
                             help="不在校园 IP 时，用它证明机构身份下全文。")
        wil = c3.text_input("Wiley TDM token", type="password", placeholder=_ph(settings.wiley_tdm_token),
                            help="下 Wiley 的全文 PDF。开放获取的都能下；订阅文章要机构订阅、且服务器在机构 IP 内。")
        s2 = c4.text_input("Semantic Scholar key（可选）", type="password", placeholder=_ph(settings.semantic_scholar_api_key),
                           help="不填也能用，填了不容易被限流。")
        mail = st.text_input("联系邮箱（Unpaywall 找开放获取全文必填；OpenAlex / Crossref 带上更快）", settings.unpaywall_email)
        if st.form_submit_button("保存", type="primary", icon=":material/save:"):
            pairs = {"BOCHA_API_KEY": _secret(bocha), "ELSEVIER_API_KEY": _secret(els), "ELSEVIER_INSTTOKEN": _secret(inst),
                     "WILEY_TDM_TOKEN": _secret(wil), "SEMANTIC_SCHOLAR_API_KEY": _secret(s2),
                     "UNPAYWALL_EMAIL": mail.strip() or None, "OPENALEX_MAILTO": mail.strip() or None}
            remove: list[str] = []
            oa_raw = oa.strip()
            if oa_raw:
                keys: list[str] = []
                for k in oa_raw.replace(",", "\n").replace("，", "\n").split():
                    if k != "-" and k not in keys:
                        keys.append(k)
                pairs["OPENALEX_API_KEYS"] = ",".join(keys)
                remove.append("OPENALEX_API_KEY")          # 旧的单个 key 并进来了，不再单独存
            set_env(pairs, remove=remove)
            if oa_raw and settings.openalex_keys:
                with st.spinner("检查 OpenAlex key…"):
                    openalex.check_keys()
            st.toast("已保存", icon=":material/check:")
            st.rerun()

    with st.container(horizontal=True):
        if st.button("检查 OpenAlex key", icon=":material/fact_check:", disabled=not settings.openalex_keys):
            with st.spinner("逐个试一下…"):
                openalex.check_keys()
            st.rerun()
        if st.button("测试 Scopus", icon=":material/bolt:", disabled=not settings.elsevier_api_key):
            st.session_state["lit_test"] = ("Scopus", *elsevier.ping())
        if st.button("测试 Wiley", icon=":material/bolt:", disabled=not settings.wiley_tdm_token):
            with st.spinner("找一篇 Wiley 开放获取文章试下载…"):
                st.session_state["lit_test"] = ("Wiley", *wiley.ping())
        if st.button("测试 Semantic Scholar", icon=":material/bolt:"):
            st.session_state["lit_test"] = ("Semantic Scholar", *semantic_scholar.ping())
    res = st.session_state.get("lit_test")
    if res:
        (st.success if res[1] else st.error)(f"{res[0]}：{res[2]}")


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
