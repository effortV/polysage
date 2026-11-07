from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from polysage import chat_widget, jobs, ui
from polysage.aba import optimize as O
from polysage.aba import prices as P
from polysage.aba import report as RP
from polysage.aba import rules as R
from polysage.aba.model import Ctx, Design, base_design
from polysage.aba.model import layer_text
from polysage.formulation import materials as MAT

_HAS_DELTA_ARROW = ui._has_param(st.metric, "delta_arrow")

ui.page_header("配方助手", "直接说要什么——要方案、问价格、找新材料、出实验——助手会自己去算、去查，需要时在后台调度研发流水线和行情更新。")


@st.cache_resource(show_spinner=False)
def _seed_reference_data() -> None:
    MAT.seed_materials()


_seed_reference_data()

_ctx = Ctx()
_ref = base_design()
_ref_ev = _ctx.evaluate(_ref, _ref)
_ll = P.price_row("LL")
_cal = R.calibration()
_job = jobs.info()


def _price_age() -> tuple[str, str | None]:
    from datetime import date

    d = P._as_date((_ll or {}).get("price_date"))
    if not d:
        return "—", None
    days = (date.today() - d).days
    return str(d), (f"{days} 天前" if days > 0 else "今天")


_pdate, _page = _price_age()
with st.container(horizontal=True):
    st.metric("现配方成本", f"{_ref_ev['cost']:,.0f} 元/吨" if _ref_ev["cost"] else "缺价格", border=True,
              help=f"{_ref.text()}；按价格卡当前价")
    st.metric("LLDPE 报价日", _pdate, delta=_page, delta_color="off", border=True,
              **({"delta_arrow": "off"} if _HAS_DELTA_ARROW else {}),
              help="价格卡上 LLDPE 那条报价的日期；超过两周建议更新贸易商日报，或让助手“按期货更新价格”")
    st.metric("性能模型", f"已校准 {_cal.get('n')} 组" if _cal else "定性先验", border=True,
              help="还没做实验时用系数表预测；回填实验数据并校准后会越来越准")
    st.metric("后台任务", jobs.JOB_NAMES.get(_job["name"], _job["name"]) if _job else "空闲",
              delta=f"{_job['elapsed'] / 60:.0f} 分钟" if _job else None, delta_color="off", border=True,
              **({"delta_arrow": "off"} if _HAS_DELTA_ARROW else {}),
              help="找新材料、文献调研、更新行情都在后台跑；进度可以直接问助手")

with st.container(horizontal=True):
    for page_path, label, icon, note in (
            ("app_pages/sourcing.py", "① 更新价格", ":material/payments:", "贴贸易商日报，或让助手按期货更新"),
            ("app_pages/agent.py", "② 要方案", ":material/auto_awesome:", "在下面直接问，或用「分档计算」"),
            ("app_pages/experiments.py", "③ 做实验", ":material/science:", "出试验方案，按投料单打样"),
            ("app_pages/experiments.py", "④ 回填校准", ":material/fact_check:", "上传实测，模型越用越准")):
        with st.container(border=True):
            st.page_link(page_path, label=label, icon=icon)
            st.caption(note)

tab_chat, tab_opt, tab_eval, tab_coef = st.tabs(["对话", "分档计算", "评估一个配方", "系数表"],
                                                 key="agent-workspace-tab", on_change="rerun")

REQ_OPTIONS = {"不降（≥ 现配方）": 1.0, "提升 5%": 1.05, "提升 10%": 1.10, "允许降 5%": 0.95, "允许降 10%": 0.90, "不管": None}
GRADE_OPTIONS = {"三档都给": None, "至少 一般": 0, "至少 好": 1, "至少 很好": 2}
GRADE_COLOR = {2: "blue", 1: "green", 0: "gray"}


def _render_tab_chat() -> None:
    sid = chat_widget.get_or_create_session("advisor", "advisor_session")
    side, main = st.columns([1.3, 3.4], gap="medium")
    with side:
        chat_widget.render_history("advisor", "advisor_session", sid)     # 新对话 + 历史对话（点开 / 删除）
    with main:
        chat_widget.render_session_chat(sid, "说要求：性能不降最低多少钱 / 三档透光各给最便宜的 / 现在价格多少 / 有没有新材料…",
                                        key="advisor_chat", examples=chat_widget.ADVISOR_EXAMPLES)


def _card(ev: dict, g: int, gname: str) -> None:
    """一档的第 1 名：一张卡片看全成本、性能、厚度与依据。"""
    with st.container(border=True):
        st.markdown(f":{GRADE_COLOR[g]}-badge[透光 ≥{R.GRADE_LABEL[g]}]　" + (f":orange-badge[把握 {ev['confidence']}]"
                                                                             if ev["confidence"] in ("低", "中低") else
                                                                             f":gray-badge[把握 {ev['confidence']}]"))
        # 降本为绿、变贵为红（delta 文本本身已经写明“降本 / 贵”）
        kw = {"delta_arrow": "off"} if _HAS_DELTA_ARROW else {}
        st.metric("元/吨", f"{ev['cost']:,.0f}", delta=O.saving_text(ev.get("saving_pct")),
                  delta_color="normal" if (ev.get("saving_pct") or 0) >= 0 else "inverse", **kw)
        d = ev["design"]
        st.markdown(f"**A {d.a_ratio:.0%}**：{layer_text(d.a)}")
        st.markdown(f"**B {1 - d.a_ratio:.0%}**：{layer_text(d.b)}")
        st.caption(f"{d.thickness:g} 丝 · 克重 {ev['gsm']:.0f} g/m² · 约 {ev.get('cost_m2') or 0:.3f} 元/m²")
        st.caption(f"纵向拉断力 {ev['md_rel']:.2f}× · 横向 {ev['td_rel']:.2f}×（相对现配方）"
                   + (f" · 比现有料最优 {ev['vs_current']:+.0f}" if gname == "加新料" and ev.get("vs_current") is not None else ""))
        with st.popover("依据", icon=":material/info:"):
            for x in ev.get("drivers") or []:
                st.caption("· " + x)


def _result_tables(out: dict) -> None:
    ref = out["ref"]
    st.caption(f"价格：{out['scenario']['label']}　·　现配方：{ref['text']}，{ref['cost'] or '缺价'} 元/吨，"
               f"克重 {ref['gsm']} g/m²，透光 {ref['grade_label']}")
    rows = O.rows(out)
    for gname, per in out["groups"].items():
        st.subheader("只用现有料" if gname == "现有料" else "加 / 换新料", anchor=False)
        firsts = [(g, items[0]) for g, items in per.items() if items]
        if not firsts:
            st.info("加/换新料都不比只用现有料更省。" if gname == "加新料"
                    else "没有满足要求的方案：放宽拉断力要求、加大厚度上限，或降低透光要求再试。")
            continue
        cols = st.columns(len(firsts))
        for col, (g, ev) in zip(cols, firsts):
            with col:
                _card(ev, g, gname)
        sub = [r for r in rows if r["分组"] == gname]
        with st.expander(f"全部备选（{len(sub)} 个）", icon=":material/table_rows:"):
            show = pd.DataFrame([{k: v for k, v in r.items() if k not in ("分组", "依据", "_ev")
                                  and not (k == "比现有料最优" and gname == "现有料")} for r in sub])
            st.dataframe(show, hide_index=True, **ui.WIDE,
                         column_config={"元/吨": st.column_config.NumberColumn(format="%.0f"),
                                        "元/m²(参考)": st.column_config.NumberColumn(format="%.3f"),
                                        "纵向拉断力": st.column_config.NumberColumn(format="%.2f×"),
                                        "横向拉断力": st.column_config.NumberColumn(format="%.2f×")})
    for n in out["notes"]:
        st.caption(n)


def _render_tab_opt() -> None:
    with st.form("aba_query"):
        c1, c2, c3 = st.columns(3)
        md = c1.selectbox("纵向拉断力", list(REQ_OPTIONS), index=0)
        td = c2.selectbox("横向拉断力", list(REQ_OPTIONS), index=0)
        grade = c3.selectbox("透光", list(GRADE_OPTIONS), index=0)
        c4, c5, c6 = st.columns(3)
        t_rng = c4.slider("厚度范围（丝）", 2.0, 8.0, (3.0, 6.0), step=0.5, help="要固定厚度就把两头拉到同一个值")
        t_ref = c5.number_input("现配方对照厚度（丝）", 2.0, 8.0, 4.5, step=0.5)
        scen = c6.selectbox("价格", list(P.SCENARIOS), format_func=P.SCENARIOS.get, index=0)
        c7, c8, c9 = st.columns(3)
        ref_f = c7.number_input("现配方 B 层已有填充 %", 0.0, 50.0, 0.0, step=5.0)
        ref_h = c8.number_input("现配方 B 层已有 HDPE %", 0.0, 15.0, 0.0, step=5.0)
        pct = c9.number_input("自定义：LLDPE 涨跌 %", -30.0, 30.0, 0.0, step=1.0, help="价格选“自定义涨跌”时生效")
        use_new = st.checkbox("允许加/换库里其它买得到的料", value=True)
        with ui.expander("更多约束"):
            a1, a2, a3, a4 = st.columns(4)
            a_rng = a1.slider("A 层占比", 0.20, 0.60, (0.30, 0.50), step=0.05)
            f_max = a2.number_input("B 层填充上限 %", 0.0, 50.0, 50.0, step=5.0)
            h_max = a3.number_input("B 层 HDPE 上限 %", 0.0, 15.0, 10.0, step=5.0)
            ar_max = a4.number_input("A 层再生上限 %", 0.0, 55.0, 33.3, step=5.0, help="表层再生太多会影响表面质量")
        go = st.form_submit_button("计算", type="primary")
    if go:
        q = O.Query(md_req=REQ_OPTIONS[md], td_req=REQ_OPTIONS[td], grade_min=GRADE_OPTIONS[grade], t_min=t_rng[0],
                    t_max=t_rng[1], t_ref=t_ref, ref_filler=ref_f, ref_hdpe=ref_h, a_min=a_rng[0], a_max=a_rng[1],
                    filler_max=f_max, hdpe_max=h_max, a_recycled_max=ar_max, use_new=use_new, scenario=scen,
                    custom_pct=pct)
        with st.spinner("计算中…"):
            st.session_state["aba_out"] = O.run(q)
    out = st.session_state.get("aba_out")
    if not out:
        st.caption("选好要求点“计算”。三档都给时，会分别列出透光 ≥很好、≥好、≥一般 时成本最低的方案。")
        return
    _result_tables(out)
    b1, b2, b3 = st.columns(3)
    if b1.button("导出 Word 报告", key="aba_export", icon=":material/description:"):
        st.session_state["aba_export_path"] = str(RP.export_docx(out))
        st.session_state["aba_export_xlsx"] = str(RP.export_xlsx(out))
    if st.session_state.get("aba_export_path"):
        p = Path(st.session_state["aba_export_path"])
        if p.exists():
            b1.download_button("下载 Word", p.read_bytes(), file_name=p.name, key="aba_dl", type="primary",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        x = Path(st.session_state.get("aba_export_xlsx") or "")
        if x.is_file():
            b1.download_button("另存表格 xlsx", x.read_bytes(), file_name=x.name, key="aba_dl_x")
    if b2.button("每档第 1 名存入配方库", key="aba_save"):
        codes = RP.save_to_library(out, origin="配方推荐页")
        b2.success("已存：" + ("、".join(codes) or "无（含买不到的料）"))
    choices = [f"{r['分组']}｜{r['透光要求']} #{r['名次']}" for r in O.rows(out)]
    pick = b3.selectbox("采购清单", ["—"] + choices, key="aba_proc_pick", label_visibility="collapsed")
    if pick != "—":
        r = O.rows(out)[choices.index(pick)]
        plan = RP.procurement_for(r["_ev"])
        st.dataframe(pd.DataFrame([{"代码": it["code"], "材料": it["name"], "整膜占比%": round(it["pct"], 2),
                                    "到厂价": it["price"], "来源": it["tier"], "供应商": it["supplier"], "小计/吨": it["subtotal"]}
                                   for it in plan["items"]]), hide_index=True, **ui.WIDE)
        st.caption(f"合计 {plan['total'] or '有料缺价'} 元/吨（按价格卡当前价）" + (f"；缺价：{'、'.join(plan['missing'])}" if plan["missing"] else ""))


def _layer_editor(label: str, default: dict[str, float], key: str) -> dict[str, float]:
    df = pd.DataFrame([{"代码": k, "%": v} for k, v in default.items()])
    ed = st.data_editor(df, num_rows="dynamic", hide_index=True, key=key, **ui.WIDE,
                        column_config={"代码": st.column_config.SelectboxColumn(options=sorted(_ctx.mats)),
                                       "%": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=1.0)})
    out = {}
    for _, r in ed.iterrows():
        if str(r["代码"] or "").strip() and r["%"]:
            out[str(r["代码"]).strip()] = float(r["%"])
    st.caption(f"{label}合计 {sum(out.values()):.1f}%")
    return out


def _render_tab_eval() -> None:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**A 层（表层，两面同配方）**")
        a = _layer_editor("A 层", {"LL": 66.7, "R1": 33.3}, "eval_a")
    with c2:
        st.markdown("**B 层（芯层）**")
        b = _layer_editor("B 层", {"LL": 53.4, "R1": 26.6, "FL": 20.0}, "eval_b")
    c3, c4, c5, c6 = st.columns(4)
    a_ratio = c3.slider("A 层占比", 0.20, 0.60, 0.45, step=0.05)
    t = c4.number_input("厚度（丝）", 2.0, 8.0, 4.5, step=0.5)
    t_ref = c5.number_input("现配方对照厚度", 2.0, 8.0, 4.5, step=0.5, key="eval_tref")
    scen = c6.selectbox("价格", list(P.SCENARIOS)[:4], format_func=P.SCENARIOS.get, key="eval_scen")
    sc = P.build(scen)
    ctx = Ctx(prices=sc["prices"])
    d = Design(a=a, b=b, a_ratio=a_ratio, thickness=t)
    ref = base_design(t_ref)
    ev, r0 = ctx.evaluate(d, ref), ctx.evaluate(ref, ref)
    m1, m2, m3, m4, m5 = ui.hrow(5)
    m1.metric("元/吨", f"{ev['cost']:,.0f}" if ev["cost"] else "缺价",
              delta=None if not (ev["cost"] and r0["cost"]) else f"{ev['cost'] - r0['cost']:+,.0f} 较现配方", delta_color="inverse")
    m2.metric("纵向拉断力", f"{ev['md_rel']:.2f}×" + (f" ≈{ev['md_N']} N" if ev.get("md_N") else ""))
    m3.metric("横向拉断力", f"{ev['td_rel']:.2f}×" + (f" ≈{ev['td_N']} N" if ev.get("td_N") else ""))
    m4.metric("透光", f"{ev['grade']} {ev['grade_label']}", help=f"透光分 {ev['trans_score']}")
    m5.metric("克重", f"{ev['gsm']} g/m²", help=f"把握：{ev['confidence']}")
    for x in ctx.drivers(d, ref, top=6):
        st.caption("· " + x)
    for issue in ev["issues"]:
        st.warning(issue)
    if ev["missing"]:
        st.warning("缺价格：" + "、".join(ev["missing"]))
    for n in sc["notes"]:
        st.caption(n)


def _render_tab_coef() -> None:
    st.caption("每类料每 10 个百分点（占整膜）对纵向/横向拉伸强度和透光分的影响。没有数据时是先验，"
               "可以按你们的经验改；实验回填校准后，被校准过的项以校准值为准。")
    t = R.load_table()
    df = pd.DataFrame([{"角色": k, "名称": v["label"], "纵向": v["md"], "横向": v["td"], "透光分": v["trans"],
                        "只能放": v["layers"], "层内上限%": v["cap"], "可信度": "已校准" if v.get("calibrated") else v["conf"],
                        "依据": v["basis"]} for k, v in t.items()])
    ed = st.data_editor(df, hide_index=True, key="coef_editor", disabled=["角色", "名称", "只能放", "层内上限%", "可信度", "依据"],
                        **ui.WIDE)
    c1, c2 = st.columns(2)
    if c1.button("保存修改", key="coef_save"):
        R.save_user_table({r["角色"]: {"md": r["纵向"], "td": r["横向"], "trans": r["透光分"]} for _, r in ed.iterrows()})
        st.success("已保存，之后的计算都用新系数。")
        st.rerun()
    if c2.button("恢复默认（清掉手改的）", key="coef_reset"):
        R.save_user_table({})
        st.rerun()
    st.caption(f"透光分 ≥{R.GRADE_CUTS[1]} 很好、≥{R.GRADE_CUTS[0]} 好、其余一般；纯 LLDPE 膜基线 {R.S0}；"
               f"每厚 1 丝透光分 {R.THICK_TRANS:+.2f}；拉断力与厚度成正比。")
    with ui.expander("材料怎么归类"):
        st.dataframe(pd.DataFrame([{"代码": c, "材料": m["name"], "归为": R.ROLES[_ctx.role[c]]["label"],
                                    "价格": _ctx.prices.get(c)} for c, m in _ctx.mats.items()]), hide_index=True, **ui.WIDE)
    if _cal:
        st.caption(f"最近一次校准：{_cal.get('date')}，{_cal.get('n')} 组实测；透光档命中 {_cal.get('grade_hit')}%。"
                   "到「实验与模型」可重新校准或撤销。")


_TAB_RENDERERS = (
    (tab_chat, _render_tab_chat),
    (tab_opt, _render_tab_opt),
    (tab_eval, _render_tab_eval),
    (tab_coef, _render_tab_coef),
)
for _tab, _renderer in _TAB_RENDERERS:
    with _tab:
        if _tab.open:
            _renderer()
