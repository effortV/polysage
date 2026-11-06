from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from polysage import ui
from polysage.aba import experiments as E
from polysage.aba import rules as R
from polysage.aba.model import layer_text

ui.page_header("实验与校准", "出试验方案 → 按投料单打样 → 回填实测 → 校准系数 → 推荐下一轮。实验越多，方案预测越准。")

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


def _download(path: str, label: str, key: str) -> None:
    p = Path(path)
    if p.exists():
        st.download_button(label, p.read_bytes(), file_name=p.name, key=key)


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


def _render_tab_plan() -> None:
    st.caption("首轮：现配方在 3.5 / 4.5 / 5.5 丝各做一组（拿到拉断力的绝对值和厚度效应），再用 D-最优挑 11 组，"
               "覆盖再生比例、B 层填充（0~45%）、B 层 HDPE、A 层占比、厚度五个因素。只用你们现有的料，先把主要系数校准准。")
    c1, c2, c3 = st.columns(3)
    t_ref = c1.number_input("现配方对照厚度（丝）", 2.0, 8.0, 4.5, step=0.5, key="plan_tref")
    n_pts = c2.number_input("D-最优组数", 6, 20, 11, key="plan_n")
    if c3.button("生成首轮试验方案", type="primary", disabled=bool(_trials), help="已有试验时请用「下一轮」"):
        r = E.first_round(t_ref=t_ref, n_points=int(n_pts))
        st.session_state["exp_path"] = r["path"]
        st.success(f"第 {r['round']} 轮 {len(r['trials'])} 组已生成。")
        st.rerun()
    if _trials:
        rounds = sorted({t["round"] for t in _trials})
        pick = st.selectbox("看哪一轮", rounds, index=len(rounds) - 1, format_func=lambda x: f"第 {x} 轮")
        _trials_table([t for t in _trials if t["round"] == pick])
        if st.button("导出这一轮方案表（含投料单与回填页）", key="plan_export"):
            st.session_state["exp_path"] = str(E.export_round(pick))
    if st.session_state.get("exp_path"):
        _download(st.session_state["exp_path"], "下载方案表 xlsx", "plan_dl")
    with ui.expander("测试与判档约定"):
        for line in E.TEST_NOTE:
            st.caption("· " + line)


def _render_tab_fill() -> None:
    st.caption("把方案表的“数据记录”页填好上传：编号、实测厚度、纵向/横向拉断力（N）、透光档（0/1/2）。空着的组会跳过。")
    up = st.file_uploader("上传回填好的方案表", type=["xlsx"], key="fill_up")
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
        st.session_state["exp_path"] = r["path"]
        st.success(f"第 {r['round']} 轮 {len(r['trials'])} 组已生成。")
        st.rerun()
    if st.session_state.get("exp_path"):
        _download(st.session_state["exp_path"], "下载最新方案表 xlsx", "next_dl")


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
