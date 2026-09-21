from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from polysage import db, kb, llm, ui
from polysage.config import UPLOAD_DIR, settings
from polysage.ingest import embed_missing, ingest_file, ingest_hit, ingest_url
import polysage.rag.answer as RAG
from polysage.rag.index import search as kb_search
from polysage.sources import DEFAULT_PROVIDERS, PROVIDERS, search_all

ui.page_header("知识库", "文献、专利、TDS 与网页资料；检索问答带出处。")

stats = db.kb_stats()
metric_cols = ui.hrow(6)
for col, (label, key) in zip(metric_cols, [("资料", "sources"), ("切片", "chunks"), ("已向量化", "chunks_embedded"),
                                        ("材料卡", "cards_material"), ("文献卡", "cards_literature"), ("专利卡", "cards_patent")]):
    col.metric(label, stats.get(key, 0))

tab_q, tab_s, tab_u, tab_c, tab_src = st.tabs(["问答", "联网检索入库", "手动上传", "卡片", "资料清单"], key="knowledge-workspace-tab", on_change="rerun")

def _render_tab_q() -> None:
    q = st.text_input("问题", placeholder="例如：茂金属 LLDPE 替代 C4 LLDPE 时对膜泡稳定性有什么影响？")
    types = st.pills("限定类型", ["paper", "patent", "tds", "web", "price"], selection_mode="multi")
    if st.button("检索并回答", disabled=not q):
        if not settings.llm_ready:
            hits = kb_search(q, top_k=8, source_types=types or None)
            st.caption("对话模型未配置，只显示检索片段。")
            for h in hits:
                st.markdown(f"**{RAG.cite_label(h['source'])}**")
                st.text(h["text"][:600])
        else:
            with st.spinner("检索与生成中…"):
                res = RAG.answer(q, source_types=types or None)
            st.markdown(res["answer"])
            with st.expander("出处", expanded=True):
                for c in res["citations"]:
                    st.markdown(f"- **[{c['label']}]** {c['cite']}")

def _render_tab_s() -> None:
    q2 = st.text_input("检索式（中英文均可；英文对学术库更有效）", key="q_search")
    provs = st.multiselect("来源", list(PROVIDERS), default=DEFAULT_PROVIDERS, format_func=lambda k: PROVIDERS[k])
    n = st.slider("每源条数", 5, 50, 15)
    if st.button("检索", disabled=not q2):
        with st.spinner("检索中…"):
            hits, errors = search_all(q2, providers=provs, limit=n)
        st.session_state["search_hits"] = hits
        if errors:
            st.warning("部分来源出错：" + "; ".join(f"{k}: {v[:80]}" for k, v in errors.items()))
    hits = st.session_state.get("search_hits") or []
    if hits:
        df = pd.DataFrame([{"选": False, "题名": h.title, "类型": h.source_type, "来源": h.provider, "年份": h.year, "期刊/申请人": h.venue[:40],
                            "DOI/URL": h.doi or h.url, "OA": bool(h.oa_pdf_url), "摘要": (h.abstract or "")[:160]} for h in hits])
        edited = st.data_editor(df, hide_index=True, disabled=[c for c in df.columns if c != "选"], height=400, **ui.WIDE)
        sel = [hits[i] for i, v in enumerate(edited["选"].tolist()) if v]
        fetch_ft = st.toggle("尽力抓取全文（OA PDF / 专利页 / 网页）", value=True)
        if st.button(f"入库选中的 {len(sel)} 条", disabled=not sel):
            prog = st.progress(0)
            for i, h in enumerate(sel):
                sid, note = ingest_hit(h, fetch_fulltext=fetch_ft)
                st.write(f"#{sid} {h.title[:60]} — {note}")
                prog.progress((i + 1) / len(sel))
            st.success("完成")

def _render_tab_u() -> None:
    st.caption("支持 PDF / DOCX / XLSX / CSV / HTML / TXT / MD；CNKI、万方导出 txt 自动拆题录。")
    files = st.file_uploader("上传资料", accept_multiple_files=True)
    c1, c2, c3 = st.columns(3)
    stype = c1.selectbox("类型", ["paper", "patent", "tds", "web", "price"])
    cred = c2.selectbox("可信度", [1, 2, 3, 4, 5, 6], index=2, format_func=lambda x: f"{x} - {RAG.CRED_LABEL[x]}")
    note = c3.text_input("备注/出处说明")
    if st.button("入库上传文件", disabled=not files):
        for f in files:
            dest = UPLOAD_DIR / f.name
            dest.write_bytes(f.getvalue())
            for sid, msg in ingest_file(dest, source_type=stype, credibility=int(cred), notes=note, copy_to_uploads=False):
                st.write(f"#{sid} {f.name} — {msg}")
    url = st.text_input("或输入网址入库（供应商 TDS 页、价格页、文章）")
    if st.button("抓取网址入库", disabled=not url):
        try:
            sid, msg = ingest_url(url, source_type=stype, credibility=int(cred))
            st.success(f"#{sid} {msg}")
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    if st.button("补建向量", help="配置 SiliconFlow 密钥后运行一次"):
        st.info(f"处理 {embed_missing()} 个切片")

def _render_tab_c() -> None:
    ctype = st.segmented_control("类型", ["material", "literature", "patent"], default="material",
                                 format_func={"material": "材料卡", "literature": "文献卡", "patent": "专利卡"}.get)
    cards = kb.list_cards(ctype)
    if not cards:
        st.caption("暂无卡片；流水线采集与寻料阶段会自动生成。")
    names = {c["id"]: c["name"] for c in cards}
    pick = st.selectbox("卡片", list(names), format_func=names.get) if cards else None
    if pick:
        card = kb.get_card(pick)
        p = Path(card["file_path"]) if card.get("file_path") else None
        st.markdown(p.read_text(encoding="utf-8") if p and p.exists() else "")
        if st.button("删除卡片"):
            kb.delete_card(pick)
            st.rerun()
    if ctype == "material":
        new_name = st.text_input("生成材料卡（材料名称）")
        if st.button("生成", disabled=not new_name or not settings.llm_ready):
            from polysage.ingest.cards import make_material_card

            try:
                cid, cits = make_material_card(new_name)
                st.success(f"已生成卡片 #{cid}")
                st.rerun()
            except llm.LLMError as e:
                st.error(str(e))
    else:
        srcs = db.list_sources(200, "patent" if ctype == "patent" else "paper")
        done = {c["source_id"] for c in cards}
        cand = [s for s in srcs if s["id"] not in done and (s.get("n_chunks") or 0) > 0]
        if cand:
            pick_src = st.selectbox("为资料生成卡片", [s["id"] for s in cand], format_func=lambda i: next(f"#{s['id']} {s['title'][:70]}" for s in cand if s["id"] == i))
            if st.button("生成卡片", disabled=not settings.llm_ready):
                from polysage.ingest.cards import make_literature_card

                cid = make_literature_card(pick_src)
                st.success(f"已生成卡片 #{cid}")
                st.rerun()

def _render_tab_src() -> None:
    rows = db.list_sources(1000)
    if rows:
        df = pd.DataFrame(rows)[["id", "source_type", "provider", "title", "authors", "year", "venue", "doi", "url", "credibility", "n_chunks", "retrieved_at"]]
        st.dataframe(df, hide_index=True, height=500, **ui.WIDE)
        del_id = st.number_input("删除资料 id", min_value=0, value=0)
        if st.button("删除", disabled=not del_id):
            db.delete("sources", int(del_id))
            st.rerun()
    else:
        st.caption("知识库为空。")


_TAB_RENDERERS = (
    (tab_q, _render_tab_q),
    (tab_s, _render_tab_s),
    (tab_u, _render_tab_u),
    (tab_c, _render_tab_c),
    (tab_src, _render_tab_src),
)
for _tab, _renderer in _TAB_RENDERERS:
    with _tab:
        if _tab.open:
            _renderer()
