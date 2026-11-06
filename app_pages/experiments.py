from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from polysage import jobs, ui
from polysage.aba import prices as P
from polysage.aba import experiments as E
from polysage.aba import rules as R
from polysage.aba.model import layer_text

ui.page_header("实验与校准", "跑一整轮出试验方案（Word）→ 按投料单打样 → 把 Word 里的记录表填好传回来 → 校准系数 → 推荐下一轮。实验越多，预测越准。")

_trials = E.trials()
_measured = E.measured()
_cal = R.calibration()
k1, k2, k3, k4 = ui.hrow(4)
k1.metric("试验", f"{len(_trials)} 组", help=f"{len({t['round'] for t in _trials})} 轮")
k2.metric("已回填", f"{len(_measured)} 组")
k3.metric("模型", f"已校准 {_cal.get('n')} 组" if _cal else "定性先验")
k4.metric("现配方拉断力", f"纵 {_cal.get('md_N_ref')} / 横 {_cal.get('td_N_ref')} N" if _cal.get("md_N_ref") else "待对照组回填",
          help="现配方在 4.5 丝下的拉断力，由对照组实测拟合")

tab_plan, tab_fill, tab_cal, tab_next = st.tabs(["试验方案", "回填实测", "校准与对比", "下一轮"],
                                                key="experiments-workspace-tab", on_change="rerun")


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
jobs.JOB_NAMES.setdefault("aba_round", "跑一整轮（行情 → 新料 → 试验方案 → Word）")


def _download(path: str, label: str, key: str, primary: bool = False) -> None:
    p = Path(path or "")
    if p.is_file():
        st.download_button(label, p.read_bytes(), file_name=p.name, key=key, type="primary" if primary else "secondary",
                           mime=DOCX_MIME if p.suffix == ".docx" else None, icon=":material/download:")


def _trials_table(rows: list[dict]) -> None:
    if not rows:
        st.caption("还没有试验。")
        return
    st.dataframe(pd.DataFrame([{
        "编号": t["code"], "轮次": t["round"], "目的": t["purpose"], "A 占比": f"{t['design'].a_ratio:.0%}",
        "厚度(丝)": t["design"].thickness, "A 层": layer_text(t["design"].a), "B 层": layer_text(t["design"].b),
        "预计元/吨": t["predicted"].get("cost"), "预测透光": t["predicted"].get("grade_label"),
        "实测纵向N": t.get("md_n"), "实测横向N": t.get("td_n"),
        "实测透光": R.GRADE_LABEL.get(t["grade"]) if t.get("grade") is not None else None,
    } for t in rows]), hide_index=True, **ui.WIDE)


@st.fragment(run_every="5s" if jobs.is_running("aba_round") else None)
def _round_job_panel() -> None:
    run = jobs.info()
    res = jobs.result("aba_round")
    if run and run["name"] == "aba_round":
        st.info(f"正在跑一整轮：更新行情 → 找新料 → 分档寻优 → 出试验方案和报告（已 {run['elapsed'] / 60:.0f} 分钟，"
                "一般 5~15 分钟）。可以先去别的页面，跑完回来下载。", icon=":material/autorenew:")
        return
    if res and res["done"]:
        if res["error"]:
            st.error(f"上次一整轮失败：{res['error']}")
        elif res["result"]:
            r = res["result"]
            st.success(f"第 {r['round']} 轮已出：{r['n']} 组（" + "，".join(f"{k} {v}" for k, v in r["counts"].items())
                       + f"），用时 {r['minutes']} 分钟。" + "".join(f" {k}：{v}。" for k, v in r["steps"].items()))
            c1, c2, c3 = st.columns(3)
            with c1:
                _download(r["plan_docx"], "下载试验方案 Word", "round_plan_dl", primary=True)
            with c2:
                _download(r["report_docx"], "下载运行报告 Word", "round_rep_dl", primary=True)
            with c3:
                _download(r["plan_xlsx"], "另存方案表 xlsx", "round_xlsx_dl")


def _render_tab_plan() -> None:
    st.markdown("**一键跑一整轮**：更新行情（期货 + 网查报价）→ 清掉抽错的价格 → 找新料 → 分档寻优（当前价 + 期货价）"
                "→ 出试验方案 → 生成《试验方案》《运行报告》两份 Word。")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1.4], vertical_alignment="bottom")
    n_all = c1.number_input("这一轮做几组", 12, 80, 50, step=2, key="round_n")
    refresh = c2.checkbox("先更新行情", value=True, key="round_refresh")
    find_new = c3.checkbox("先找新料", value=True, key="round_new")
    if c4.button("开始跑一整轮", type="primary", icon=":material/play_arrow:", disabled=jobs.is_running()):
        from polysage.aba import campaign

        n = int(n_all)
        ok = jobs.start("aba_round", lambda: campaign.run(n, refresh_market=refresh, find_new=find_new))
        st.toast("已在后台开始" if ok else "后台有别的任务在跑，等它跑完再开始", icon=":material/autorenew:")
        st.rerun()
    _round_job_panel()
    with ui.expander("只出试验方案（不更新行情、不找新料，几秒钟）"):
        st.caption("组成：现配方 3.5 / 4.5 / 5.5 丝对照 3 组 + 各透光档预测最省（降本 / 提透光）+ 库里更省的新料 + "
                   "其余用 D-最优在 A/B 层再生、B 层填充、B 层 HDPE、A 层占比、厚度六个因素上铺开（给机器学习用）。")
        a1, a2, a3 = st.columns(3)
        t_ref = a1.number_input("现配方对照厚度（丝）", 2.0, 8.0, 4.5, step=0.5, key="plan_tref")
        n_pts = a2.number_input("组数", 12, 80, 50, step=2, key="plan_n")
        scen = a3.selectbox("按哪个价格挑最省", list(P.SCENARIOS)[:4], format_func=P.SCENARIOS.get, key="plan_scen")
        if st.button("生成试验方案", key="plan_go"):
            with st.spinner("挑选中…"):
                r = E.plan_round(int(n_pts), t_ref=t_ref, scenario=scen)
            st.session_state["exp_path"] = r["docx"]
            st.session_state["exp_xlsx"] = r["xlsx"]
            st.session_state["plan_msg"] = (f"第 {r['round']} 轮 {len(r['trials'])} 组已生成："
                                            + "，".join(f"{k} {v}" for k, v in r["meta"]["counts"].items()))
            st.rerun()
    if st.session_state.get("plan_msg"):
        st.success(st.session_state.pop("plan_msg"))
    if st.session_state.get("exp_path"):
        d1, d2 = st.columns(2)
        with d1:
            _download(st.session_state["exp_path"], "下载方案 Word", "plan_dl", primary=True)
        with d2:
            _download(st.session_state.get("exp_xlsx"), "另存方案表 xlsx", "plan_dl_x")
    if _trials:
        st.divider()
        rounds = sorted({t["round"] for t in _trials})
        pick = st.selectbox("看哪一轮", rounds, index=len(rounds) - 1, format_func=lambda x: f"第 {x} 轮")
        _trials_table([t for t in _trials if t["round"] == pick])
        b1, b2 = st.columns(2)
        if b1.button("重新导出这一轮的方案 Word", key="plan_export"):
            st.session_state["exp_path"] = str(E.export_round_docx(pick))
            st.session_state["exp_xlsx"] = str(E.export_round(pick))
            st.rerun()
        if b2.button("生成这一轮的运行报告 Word", key="plan_report"):
            from polysage.aba import campaign

            with st.spinner("按当前价和期货价重新算各档最省方案…"):
                st.session_state["exp_report"] = str(campaign.report_docx(pick))
        if st.session_state.get("exp_report"):
            _download(st.session_state["exp_report"], "下载运行报告 Word", "plan_rep_dl", primary=True)
    with ui.expander("测试与判档约定"):
        for line in E.TEST_NOTE:
            st.caption("· " + line)


def _render_tab_fill() -> None:
    st.caption("把方案 Word 最后的“数据记录表”填好直接上传（Excel 方案表的“数据记录”页也行）："
               "实测厚度、纵向/横向拉断力（N）、透光档（0/1/2）。没做的组空着会跳过。")
    up = st.file_uploader("上传回填好的方案（Word 或 Excel）", type=["docx", "xlsx"], key="fill_up")
    if up is not None and st.button("导入", type="primary", key="fill_go"):
        try:
            res = E.import_results(up)
            st.success(f"写入 {res['updated']} 组；跳过（没填）{len(res['skipped'])} 组。")
            for p in res["problems"]:
                st.warning(p)
        except Exception as e:  # noqa: BLE001
            st.error(f"导入失败：{e}")
    st.markdown("##### 已回填")
    _trials_table(_measured)


def _render_tab_cal() -> None:
    st.caption("用已回填的组把系数往数据拉：数据少时系数基本不动（岭回归往先验收缩），数据多了才明显改。"
               "现配方对照组决定拉断力的绝对值。至少 4 组才校准。")
    c1, c2 = st.columns(2)
    if c1.button("用现有数据校准", type="primary", key="cal_go", disabled=len(_measured) < 4):
        r = E.calibrate()
        if r.get("ok"):
            st.success(f"已校准：现配方 4.5 丝 纵 {r.get('md_N_ref')} N / 横 {r.get('td_N_ref')} N；"
                       f"拟合误差 纵 {r.get('md_rmse_pct')}% 横 {r.get('td_rmse_pct')}%；透光档命中 {r.get('grade_hit')}%。")
        else:
            st.warning(r.get("reason"))
        st.rerun()
    if c2.button("撤销校准（回到先验）", key="cal_reset", disabled=not _cal):
        E.reset_calibration()
        st.rerun()
    if _cal:
        rows = []
        prior = R.load_table(include_cal=False)
        for role, v in (_cal.get("roles") or {}).items():
            p = prior.get(role, {})
            rows.append({"料": p.get("label", role), "用到的组数": v.get("n"),
                         "纵向 先验→校准": f"{p.get('md', 0):+.3f} → {v.get('md', p.get('md', 0)):+.3f}",
                         "横向 先验→校准": f"{p.get('td', 0):+.3f} → {v.get('td', p.get('td', 0)):+.3f}",
                         "透光 先验→校准": f"{p.get('trans', 0):+.2f} → {v.get('trans', p.get('trans', 0)):+.2f}"})
        st.dataframe(pd.DataFrame(rows), hide_index=True, **ui.WIDE)
    st.markdown("##### 预测 vs 实测")
    df = E.compare()
    if df.empty:
        st.caption("还没有回填数据。")
    else:
        st.dataframe(df, hide_index=True, **ui.WIDE)
        if df.attrs.get("note"):
            st.caption(df.attrs["note"])


def _render_tab_next() -> None:
    st.caption("下一轮 = 每档预测最省的几组（利用）+ 离已做过的配方最远、但预测可行的几组（探索）+ 现配方对照。"
               "没校准时也能出，但建议先把首轮做完、回填、校准。")
    opts = {"不降": 1.0, "提升 5%": 1.05, "允许降 5%": 0.95, "不管": None}
    c1, c2, c3, c4 = st.columns(4)
    md = c1.selectbox("纵向拉断力", list(opts), key="next_md")
    td = c2.selectbox("横向拉断力", list(opts), key="next_td")
    n_exp = c3.number_input("利用组数", 3, 12, 6, key="next_exploit")
    use_new = c4.checkbox("允许新料", value=False, key="next_new")
    if st.button("生成下一轮", type="primary", key="next_go"):
        r = E.next_round({"md_req": opts[md], "td_req": opts[td], "use_new": use_new}, n_exploit=int(n_exp))
        st.session_state["exp_path"] = r["docx"]
        st.session_state["exp_xlsx"] = r["xlsx"]
        st.success(f"第 {r['round']} 轮 {len(r['trials'])} 组已生成。")
        st.rerun()
    if st.session_state.get("exp_path"):
        _download(st.session_state["exp_path"], "下载最新方案 Word", "next_dl", primary=True)
        _download(st.session_state.get("exp_xlsx"), "另存方案表 xlsx", "next_dl_x")


_TAB_RENDERERS = (
    (tab_plan, _render_tab_plan),
    (tab_fill, _render_tab_fill),
    (tab_cal, _render_tab_cal),
    (tab_next, _render_tab_next),
)
for _tab, _renderer in _TAB_RENDERERS:
    with _tab:
        if _tab.open:
            _renderer()
