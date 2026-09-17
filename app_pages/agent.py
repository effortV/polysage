from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from polysage import chat_widget, pricing, recommender, ui
from polysage.agents import runner
from polysage.config import settings
from polysage.formulation import constraints as C
from polysage.formulation import cost as COST
from polysage.formulation import library as L
from polysage.formulation import materials as MAT
from polysage.formulation import screening as S
from polysage.pipeline import task

ui.page_header("配方推荐", "输入材料与价格，给出可试验的降本配方；价格变动即时重算。")
MAT.seed_materials()
L.seed_top20()

t = task.load()
c = C.load()
base = C.base_formulation(c)
base_cost = COST.compute_simple(base)
from polysage import basedata

pb = pricing.basis_summary()
last = pb.get("last") or {}
k1, k2, k3, k4 = ui.hrow(4)
k1.metric("现配方", COST.format_components(base))
k2.metric("现配方成本", f"{base_cost:,.0f} 元/吨" if base_cost else "缺价格")
k3.metric("成本上限 / 目标", f"{c['cost_limit']:,} / {c['cost_target']:,}")
k4.metric("价格基准", f"{pb['n_actual']} 实价 · {pb['n_estimate']} 估计价", help=f"最近更新 {last.get('created_at', '—')}；实价优先于估计价，同档取最新。对话、原料页、询价回填、网查刷新写同一张价格表。")
if basedata.is_ready():
    st.caption("base：" + basedata.brief())
else:
    st.caption("base 未录入：方案只有定性预期，不判过关。到「实验与模型 › base 现用膜」录入，或在对话里直接报四个数。")

tab_chat, tab_in, tab_out, tab_scr, tab_hist = st.tabs(["对话", "表单", "结果", "替代品筛选", "历史"])

with tab_chat:
    sid = chat_widget.get_or_create_session("advisor", "advisor_session")
    c1, c2 = st.columns([1, 5])
    if c1.button("新对话", key="new_advisor"):
        st.session_state["advisor_session"] = runner.create_session("advisor")
        st.rerun()
    chat_widget.render_session_chat(sid, "报材料与价格、要方案、改价格、追问依据", key="advisor_chat", examples=chat_widget.ADVISOR_EXAMPLES)

with tab_in:
    st.markdown("**目标膜**")
    st.text(task.brief(t))
    st.markdown("**可用材料与价格**")
    st.caption("默认为原料库全部启用材料；可改价格、增删行，或上传 JSON。")
    mats = MAT.list_materials(active_only=True)
    df = pd.DataFrame([{"用": True, "代码": m["code"], "材料": m["name"], "密度": m["density"], "熔点℃": m.get("melting_point"), "MFR": m["mfi"],
                        "共聚单体": m["comonomer"], "再生": bool(m["is_recycled"]), "价格(元/吨)": m["price"], "价格口径": m["price_tier"],
                        "新价格": None, "新价格类型": "", "价格来源": ""} for m in mats])
    edited = st.data_editor(df, hide_index=True, height=420, num_rows="dynamic", key="agent_mats",
                            column_config={"新价格类型": st.column_config.SelectboxColumn(options=["", "actual", "estimate"]),
                                           "价格口径": st.column_config.TextColumn(disabled=True)}, **ui.WIDE)
    up = st.file_uploader("上传输入 JSON", type=["json"], key="agent_json")
    c1, c2, c3, c4 = ui.hrow(4)
    only_listed = c1.toggle("只用勾选的材料出方案", value=False)
    n_schemes = c2.slider("方案数", 5, 40, 20)
    use_llm = c3.toggle("四项性能定性评估", value=settings.llm_ready, disabled=not settings.llm_ready)
    use_ml = c4.toggle("有模型时用模型打分", value=True)
    st.download_button("示例输入 JSON", json.dumps(recommender.example_input(), ensure_ascii=False, indent=2).encode("utf-8"),
                       file_name="agent_input_example.json")
    if st.button("生成方案", type="primary"):
        if up is not None:
            inp = json.loads(up.getvalue().decode("utf-8"))
        else:
            materials = []
            for _, r in edited.iterrows():
                code = str(r["代码"] or "").strip()
                if not code or not bool(r["用"]):
                    continue
                m = {"code": code, "name": r["材料"] or code, "density": r["密度"], "melting_point": r["熔点℃"], "mfi": r["MFR"],
                     "comonomer": r["共聚单体"], "is_recycled": int(bool(r["再生"]))}
                if pd.notna(r["新价格"]) and r["新价格"] not in ("", None) and float(r["新价格"]) > 0:
                    m.update({"price": float(r["新价格"]), "price_type": r["新价格类型"] or "estimate", "price_source": r["价格来源"] or "界面登记"})
                materials.append({k: v for k, v in m.items() if not (isinstance(v, float) and pd.isna(v))})
            inp = {"product": {"structure": t["product"]["structure"], "name": t["product"]["name"]},
                   "materials": materials, "only_listed_materials": only_listed, "n_schemes": n_schemes, "use_llm": use_llm, "use_ml": use_ml}
        with st.spinner("筛选替代品、生成候选、核算成本、评估排序…"):
            out = recommender.recommend(inp)
        st.session_state["agent_out"] = out
        st.success(f"{len(out['schemes'])} 个方案 · {out['mode']}")

out = st.session_state.get("agent_out")
if out is None:
    runs = recommender.last_runs(1)
    if runs:
        out = recommender.load_run(runs[0]["id"])

with tab_out:
    if not out:
        st.caption("还没有运行记录。")
    else:
        st.caption(f"{out['at'][:16].replace('T', ' ')} · {out['mode']} · 现配方成本 {out['base_cost']:.0f} · 上限 {out['cost_limit']}")
        for n in out.get("notes", []):
            st.caption("· " + n)
        rows = []
        for r in out["schemes"]:
            ml = r.get("ml") or {}
            rows.append({"排名": r["rank"], "思路": r["theme"], "配方": r["formula"], "成本": r["cost"], "口径": r["cost_tier"],
                         "降本(元/吨)": round(r["savings"]) if r.get("savings") is not None else None,
                         "降本%": round(r["savings_pct"], 1) if r.get("savings_pct") is not None else None, "面积指数": r.get("area_index"),
                         "四项(拉撕穿封)": r.get("effects_short"), "预期": r.get("expected"), "把握": r.get("pass_confidence"),
                         "P(过关)": round(ml["p_pass"], 2) if ml.get("p_pass") is not None else None, "风险": r.get("risks"), "依据": r.get("evidence")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, height=520, **ui.WIDE)
        if out.get("experiments", {}).get("picks"):
            st.markdown("##### 首轮试验建议")
            st.write(out["experiments"].get("advice", ""))
            st.dataframe(pd.DataFrame(out["experiments"]["picks"]), hide_index=True, **ui.WIDE)
        for p in out.get("outputs", []):
            pp = Path(p)
            if pp.exists():
                st.download_button(pp.name, pp.read_bytes(), file_name=pp.name, key=f"dl_agent_{pp.name}")

with tab_scr:
    ref = st.selectbox("基准材料", [m["code"] for m in MAT.list_materials()], index=0, format_func=lambda k: f"{k} {MAT.get_material(k)['name']}")
    tol_d = st.slider("密度窗口 ±", 0.002, 0.02, 0.006, step=0.001, format="%.3f")
    tol_m = st.slider("熔点窗口 ±℃", 2.0, 15.0, 6.0, step=1.0)
    res = S.screen(reference=ref, tol={"density": tol_d, "melting_point": tol_m})
    st.caption(f"基准 {res['reference']['name']}：密度 {res['reference']['density']}，熔点 {res['reference']['melting_point']}，MFR {res['reference']['mfi']}，价格 {res['reference']['price']}；"
               f"窗口 {res['windows']}")
    st.dataframe(pd.DataFrame(S.to_rows(res)), hide_index=True, height=520, **ui.WIDE)

with tab_hist:
    runs = recommender.last_runs(20)
    if runs:
        st.dataframe(pd.DataFrame(runs), hide_index=True, **ui.WIDE)
        pick = st.selectbox("查看某次运行", [r["id"] for r in runs], format_func=lambda i: f"#{i} " + next(r["at"] for r in runs if r["id"] == i))
        if st.button("载入"):
            st.session_state["agent_out"] = recommender.load_run(int(pick))
            st.rerun()
    ev = pricing.events(10)
    if ev:
        st.markdown("##### 价格事件")
        st.dataframe(pd.DataFrame([{"时间": e["at"], "触发": e["trigger"], "base成本": round(e["summary"].get("base_cost") or 0),
                                    "明显变化": e["summary"].get("n_flagged")} for e in ev]), hide_index=True, **ui.WIDE)
