from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from polysage import basedata, jobs, ui
from polysage.config import KB
from polysage.formulation import library as L
from polysage.formulation.materials import list_materials
from polysage.ml import dataset as D
from polysage.ml import models as M
from polysage.pipeline import runner, stage_experiment, state

ui.page_header("实验与模型", "base 现用膜、数据回灌、试验包、DOE、模型与推荐。")

tab_base, tab_data, tab_kit, tab_d, tab_m, tab_r = st.tabs(["base 现用膜", "数据回灌", "方案试验包", "首轮 DOE", "模型", "推荐与复盘"], key="experiments-workspace-tab", on_change="rerun")

# ---------------- base ----------------
def _render_tab_base() -> None:
    st.caption("四个数：拉伸强度、撕裂强度、穿刺力、热封强度（不分 MD/TD）。SD 与条数可后补，录入后自动派生过关线。")
    cur = basedata.load()
    items = cur.get("items", {}) or {}
    with st.form("base_form"):
        c1, c2, c3 = st.columns(3)
        th = c1.number_input("膜厚 μm", 0.0, 500.0, float(cur.get("thickness_um") or 0.0), step=1.0)
        seal_temp = c2.number_input("热封测试温度 ℃（可选）", 0.0, 300.0, float(cur.get("seal_temp") or 0.0), step=1.0)
        note = c3.text_input("说明（测试标准 / 样品批次）", cur.get("note", ""))
        vals: dict[str, dict] = {}
        for key, label, unit in basedata.ITEMS:
            v = items.get(key, {})
            a, b, c = st.columns([2, 1, 1])
            mean = a.number_input(f"{label}（{unit}）", 0.0, 1e6, float(v.get("mean") or 0.0), step=0.1, format="%.3f", key=f"b_{key}")
            sd = b.number_input("SD", 0.0, 1e6, float(v.get("sd") or 0.0), step=0.01, format="%.3f", key=f"sd_{key}")
            n = c.number_input("条数", 0, 100, int(v.get("n") or 0), key=f"n_{key}")
            vals[key] = {"mean": mean or None, "sd": sd or None, "n": n or None}
        if st.form_submit_button("保存 base", type="primary"):
            d = basedata.set_items(vals, thickness_um=th or None, seal_temp=seal_temp or None, note=note)
            st.success("已保存。" + ("四项齐全，可判过关。" if basedata.is_ready(d) else "仍缺：" + "、".join(basedata.missing(d))))
            st.rerun()
    st.markdown(basedata.to_markdown())

# ---------------- 数据回灌（两条通道）----------------
def _render_tab_data() -> None:
    if not D.TEMPLATE_PATH.exists():
        D.write_template()
    st.download_button("数据表模板", D.TEMPLATE_PATH.read_bytes(), file_name=D.TEMPLATE_PATH.name)
    st.caption("一行一次测量；配方列合计 100；再生料批号必填。两条通道同一格式，只是来源标记不同。")
    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            st.markdown("##### 推荐方案试验数据")
            codes = [f["code"] for f in L.list_formulations() if f["status"] in ("候选", "推荐", "试验中")]
            code = st.selectbox("对应方案编号", codes, key="up_code") if codes else None
            up1 = st.file_uploader("上传（CSV/XLSX）", type=["csv", "xlsx"], key="up_scheme")
            if up1 is not None and code and st.button("并入训练集（推荐方案）"):
                df_new = pd.read_excel(up1) if up1.name.endswith("xlsx") else pd.read_csv(up1, encoding="utf-8-sig")
                merged, issues = D.merge_upload(df_new, source="scheme", scheme_code=code)
                L.set_status(code, "试验中")
                st.success(f"已并入：新增 {len(df_new)} 行，训练集共 {len(merged)} 行")
                for i in issues:
                    st.warning(i)
                st.session_state["auto_train_hint"] = True
    with c2:
        with st.container(border=True):
            st.markdown("##### 自主实验数据")
            up2 = st.file_uploader("上传（CSV/XLSX）", type=["csv", "xlsx"], key="up_own")
            if up2 is not None and st.button("并入训练集（自主实验）"):
                df_new = pd.read_excel(up2) if up2.name.endswith("xlsx") else pd.read_csv(up2, encoding="utf-8-sig")
                merged, issues = D.merge_upload(df_new, source="own")
                st.success(f"已并入：新增 {len(df_new)} 行，训练集共 {len(merged)} 行")
                for i in issues:
                    st.warning(i)
                st.session_state["auto_train_hint"] = True
    df = D.load()
    if df.empty:
        st.caption("训练集为空。")
    else:
        agg = D.aggregate(df)
        n_samples = int(agg["sample_id"].nunique())
        by_src = df.groupby("source")["sample_id"].nunique().to_dict()
        st.write(f"训练集：{len(df)} 行，{n_samples} 个样品（{'，'.join(f'{k} {v}' for k, v in by_src.items())}）")
        for i in D.qc(df):
            st.warning(i)
        st.dataframe(agg, hide_index=True, height=320, **ui.WIDE)
        if n_samples >= 15:
            st.caption(f"样品数 {n_samples}，可以建模。")
            if st.button("训练并验证", disabled=jobs.is_running()):
                jobs.start("learn", lambda: (runner.run_stage("learn", echo=None), stage_experiment.record_round(len(state.load().get("rounds", [])) + 1)))
                st.success("已在后台开始训练，稍后在「模型」查看验证报告")
        else:
            st.caption(f"样品数 {n_samples}，不足 15 组暂不建模（首轮建议 24～30 组）。")
        if not basedata.is_ready():
            st.caption("base 未录入，无法判过关。")

# ---------------- 方案试验包 ----------------
def _render_tab_kit() -> None:
    st.caption("按方案生成称料单与预填配方的数据表模板；测完在「数据回灌」上传。")
    forms = [f for f in L.list_formulations() if f["status"] in ("候选", "推荐", "试验中")]
    if forms:
        pick = st.selectbox("方案", [f["code"] for f in forms], format_func=lambda c: f"{c}  {next(f['formula'] for f in forms if f['code'] == c)}")
        c1, c2 = st.columns(2)
        kg = c1.number_input("批次公斤数", 1.0, 5000.0, 25.0, step=5.0)
        ns = c2.number_input("样品数（取样时段）", 1, 10, 3)
        if st.button("生成试验包"):
            paths = D.make_trial_kit(pick, batch_kg=kg, n_samples=int(ns))
            for p in paths:
                st.download_button(f"下载 {p.name}", p.read_bytes(), file_name=p.name, key=f"kit_{p.name}")
            st.success("已保存到 06_实验数据/方案试验包/")
    else:
        st.caption("配方库为空，先在「配方推荐」出方案。")

# ---------------- 首轮 DOE ----------------
def _render_tab_d() -> None:
    if stage_experiment.DESIGN_PATH.exists():
        design = pd.read_excel(stage_experiment.DESIGN_PATH, sheet_name="试验配方")
        st.dataframe(design, hide_index=True, height=400, **ui.WIDE)
        st.download_button("下载首轮试验方案", stage_experiment.DESIGN_PATH.read_bytes(), file_name=stage_experiment.DESIGN_PATH.name)
    else:
        st.caption("尚未生成。")
    if st.button("重新生成"):
        stage_experiment.run_doe(echo=None)
        st.rerun()

# ---------------- 模型 ----------------
def _render_tab_m() -> None:
    rows = M.current_models()
    if rows:
        st.dataframe(pd.DataFrame([{"性能": r["target"], "模型": r["model_type"], "样本": r["n_samples"],
                                    "LOOCV RMSE": round(r["metrics"].get(r["model_type"], {}).get("rmse", float("nan")), 4),
                                    "R²(LOOCV)": round(r["metrics"].get(r["model_type"], {}).get("r2_loocv", float("nan")), 3),
                                    "训练时间": r["trained_at"]} for r in rows]), hide_index=True, **ui.WIDE)
    else:
        st.caption("尚无模型。")
    rep = KB["model"] / "模型验证报告.md"
    if rep.exists():
        with ui.expander("模型验证报告", expanded=True):
            st.markdown(rep.read_text(encoding="utf-8"))
    st.markdown("##### 预测配方")
    st.caption("格式如 LLC 49, LD 10, HD 5, R1 35, AD 1")
    txt = st.text_input("配方")
    if txt and rows:
        comps = {}
        for part in txt.replace("/", ",").split(","):
            k, v = part.split()
            comps[k.strip()] = float(v)
        mats = {m["code"]: m for m in list_materials(active_only=False)}
        pred = M.predict([comps], mats=mats)
        st.dataframe(pred.round(3), hide_index=True)
        if basedata.is_ready():
            j = basedata.judge({c[:-5]: float(pred.iloc[0][c]) for c in pred.columns if c.endswith("_mean")})
            st.write("过关判定：", "✅ 过关" if j["pass"] else "❌ 未过关", j["items"])

# ---------------- 推荐与复盘 ----------------
def _render_tab_r() -> None:
    if stage_experiment.RECO_PATH.exists():
        rec = pd.read_excel(stage_experiment.RECO_PATH, sheet_name="推荐")
        st.dataframe(rec, hide_index=True, **ui.WIDE)
        pf = pd.read_excel(stage_experiment.RECO_PATH, sheet_name="Pareto前沿")
        if not pf.empty and "cost" in pf and "min_margin" in pf:
            st.scatter_chart(pf.rename(columns={"cost": "成本(元/吨)", "min_margin": "最小裕度"}), x="成本(元/吨)", y="最小裕度")
        st.download_button("下载推荐表", stage_experiment.RECO_PATH.read_bytes(), file_name=stage_experiment.RECO_PATH.name)
    else:
        st.caption("尚无扫描推荐；回灌数据并建模后在「研发流水线」运行扫描。")
    reviews = sorted(KB["review"].glob("*.md"))
    if reviews:
        pick = st.selectbox("复盘纪要", reviews, format_func=lambda p: p.name)
        st.markdown(Path(pick).read_text(encoding="utf-8"))


_TAB_RENDERERS = (
    (tab_base, _render_tab_base),
    (tab_data, _render_tab_data),
    (tab_kit, _render_tab_kit),
    (tab_d, _render_tab_d),
    (tab_m, _render_tab_m),
    (tab_r, _render_tab_r),
)
for _tab, _renderer in _TAB_RENDERERS:
    with _tab:
        if _tab.open:
            _renderer()
