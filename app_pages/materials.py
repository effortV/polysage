from __future__ import annotations

import pandas as pd
import streamlit as st

from polysage import db, pricing, ui
from polysage.config import KB, settings
from polysage.formulation import constraints as C
from polysage.formulation import cost as COST
from polysage.formulation import export as X
from polysage.formulation import generator as G
from polysage.formulation import library as L
from polysage.formulation import materials as MAT
from polysage.formulation import qualitative as Q

ui.page_header("原料与价格", "原料库、价格卡与联动、配方库、候选生成、约束。")
MAT.seed_materials()
L.seed_top20()

tab_m, tab_p, tab_f, tab_g, tab_c = st.tabs(["原料库", "价格卡与回填", "配方库", "生成与评估", "约束"])

with tab_m:
    mats = MAT.list_materials(active_only=False)
    st.caption("物性以供应商 TDS 为准；种子熔点为通用典型值，请按 TDS 校正。")
    df = pd.DataFrame([{"代码": m["code"], "类别": m["category"], "材料": m["name"], "牌号": m["grade"], "生产商": m.get("producer"), "供应商": m["supplier"],
                        "MFR": m["mfi"], "密度": m["density"], "熔点℃": m.get("melting_point"), "Vicat℃": m.get("vicat"), "共聚单体": m["comonomer"],
                        "落镖g(TDS)": m.get("tds_dart"), "热封起始℃(TDS)": m.get("tds_seal_init"), "需PPA": bool(m.get("needs_ppa") or 0),
                        "再生": bool(m["is_recycled"]), "作用": m["role"], "用量下限": m["typical_min"], "用量上限": m["typical_max"],
                        "风险": m["risk"], "用于配方": m["use_flag"], "货源": m.get("availability"), "启用": bool(m["active"])} for m in mats])
    edited = st.data_editor(df, hide_index=True, height=520, key="mat_editor", num_rows="dynamic", **ui.WIDE)
    if st.button("保存原料库修改"):
        for _, r in edited.iterrows():
            if not str(r["代码"] or "").strip():
                continue
            MAT.upsert_material({"code": str(r["代码"]).strip(), "category": r["类别"], "name": r["材料"] or r["代码"], "grade": r["牌号"], "producer": r["生产商"],
                                 "supplier": r["供应商"], "mfi": r["MFR"], "density": r["密度"], "melting_point": r["熔点℃"], "vicat": r["Vicat℃"],
                                 "comonomer": r["共聚单体"], "tds_dart": r["落镖g(TDS)"], "tds_seal_init": r["热封起始℃(TDS)"], "needs_ppa": int(bool(r["需PPA"])),
                                 "is_recycled": int(bool(r["再生"])), "role": r["作用"], "typical_min": r["用量下限"], "typical_max": r["用量上限"],
                                 "risk": r["风险"], "use_flag": r["用于配方"], "availability": r["货源"], "active": int(bool(r["启用"]))})
        st.success("已保存")
    with ui.expander("再生料批次记录"):
        b = MAT.list_recycled_batches()
        if b:
            st.dataframe(pd.DataFrame(b).drop(columns=["created_at"]), hide_index=True, **ui.WIDE)
        with st.form("batch_form"):
            c1, c2, c3, c4 = st.columns(4)
            code_b = c1.selectbox("材料", [m["code"] for m in mats if m["is_recycled"]] or ["R1"])
            supplier_b = c2.text_input("供应商")
            grade_b = c3.text_input("等级/来源膜")
            batch_b = c4.text_input("批号")
            c5, c6, c7, c8, c9 = st.columns(5)
            mfr_b = c5.number_input("MFR", 0.0, 50.0, 0.0, step=0.1)
            dens_b = c6.number_input("密度", 0.0, 2.0, 0.0, step=0.001, format="%.3f")
            ash_b = c7.number_input("灰分%", 0.0, 50.0, 0.0, step=0.05)
            gel_b = c8.number_input("凝胶数/m²", 0.0, 10000.0, 0.0, step=1.0)
            price_b = c9.number_input("价格 元/吨", 0.0, 50000.0, 0.0, step=50.0)
            note_b = st.text_input("DSC 杂质 / 气味 / 备注")
            if st.form_submit_button("登记批次"):
                MAT.add_recycled_batch({"material_code": code_b, "supplier": supplier_b, "grade": grade_b, "batch_no": batch_b,
                                        "mfr": mfr_b or None, "density": dens_b or None, "ash": ash_b or None, "gel_count": gel_b or None,
                                        "price": price_b or None, "note": note_b, "received_at": pd.Timestamp.now().strftime("%Y-%m-%d")})
                st.success("已登记")
                st.rerun()

with tab_p:
    mats = MAT.list_materials(active_only=False)
    pdf = pd.DataFrame([{"代码": m["code"], "材料": m["name"], "估计价": m["price_estimate"], "实价": m["price_actual"], "采用": m["price_tier"]} for m in mats])
    st.dataframe(pdf, hide_index=True, **ui.WIDE)
    with st.container(border=True):
        st.markdown("##### 登记价格")
        c1, c2, c3, c4 = st.columns(4)
        code = c1.selectbox("材料", [m["code"] for m in mats], format_func=lambda k: f"{k} {next(m['name'] for m in mats if m['code'] == k)}")
        price = c2.number_input("价格（元/吨）", 0.0, 500000.0, 0.0, step=50.0)
        ptype = c3.selectbox("类型", ["actual", "estimate"], format_func={"actual": "甲方实价", "estimate": "网查估计价"}.get)
        src = c4.text_input("来源（供应商 / 网站 + 日期）")
        if st.button("登记", disabled=price <= 0 or not src):
            res = pricing.record_price(code, price, ptype, source=src)
            st.success(f"已登记并联动：现配方成本 {res['base_cost']:.0f}，{res['n_flagged']} 个配方成本/排名明显变化（见价格变动报告）")
            st.rerun()
    with st.container(border=True):
        st.markdown("##### 询价清单回填")
        st.caption("上传填好的《询价清单.xlsx》，读取“代码 / 到厂含税价 / 供应商 / 起订量 / 报价日期”。")
        up = st.file_uploader("询价清单", type=["xlsx", "csv"], key="inq")
        if up is not None and st.button("导入实价"):
            df_in = pd.read_excel(up) if up.name.endswith("xlsx") else pd.read_csv(up)
            res = pricing.import_actual_prices(df_in.to_dict("records"))
            st.success(f"导入 {res['n_imported']} 条实价并联动：现配方成本 {res['base_cost']:.0f}，{res['n_flagged']} 个配方明显变化；到“智能体”页重新出方案")
    with st.container(border=True):
        st.markdown("##### 价格联动")
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button("网查刷新参考价", disabled=not settings.llm_ready, help="逐个材料搜索价格页并抽取；偏差 > 40% 进入待核对"):
                with st.spinner("刷新中…"):
                    res = pricing.refresh_estimates()
                st.success(f"检查 {res['n_checked']} 项，{len(res['changed_prices'])} 项变化")
            st.caption(f"上次自动刷新：{pricing.last_refresh() or '从未'}；自动刷新默认关闭（.env PRICE_AUTO_REFRESH_DAYS）")
        with c2:
            ev = pricing.events(5)
            if ev:
                st.dataframe(pd.DataFrame([{"时间": e["at"], "触发": e["trigger"], "base成本": round(e["summary"].get("base_cost") or 0),
                                            "明显变化": e["summary"].get("n_flagged")} for e in ev]), hide_index=True, **ui.WIDE)
        st.caption("网查只更新估计价；实价来自回填或手工登记。成本计算实价优先、同档取最新；网查价偏差 > 40% 进入待核对。")
        cand_rows = db.q("SELECT id, material_code, price, price_date, source, url, created_at FROM prices WHERE price_type='estimate_candidate' ORDER BY id DESC LIMIT 50")
        if cand_rows:
            st.markdown("##### 待核对的网查参考价")
            cdf = pd.DataFrame(cand_rows)
            cdf["现估计价"] = [MAT.current_prices().get(r["material_code"], {}).get("estimate") for r in cand_rows]
            st.dataframe(cdf[["id", "material_code", "price", "现估计价", "price_date", "source", "url"]], hide_index=True, **ui.WIDE)
            a1, a2, a3 = st.columns(3)
            pick_c = a1.selectbox("选择记录", [r["id"] for r in cand_rows], format_func=lambda i: next(f"#{r['id']} {r['material_code']} {r['price']:.0f}" for r in cand_rows if r["id"] == i))
            if a2.button("采用为估计价（联动重算）"):
                row = next(r for r in cand_rows if r["id"] == pick_c)
                pricing.record_price(row["material_code"], row["price"], "estimate", source=row["source"] or "网查（人工确认）", url=row["url"] or "", price_date=row["price_date"] or "")
                db.delete("prices", int(pick_c))
                st.success("已采用并联动")
                st.rerun()
            if a3.button("忽略该记录"):
                db.delete("prices", int(pick_c))
                st.rerun()
        if pricing.REPORT_PATH.exists():
            with st.expander("最新价格变动报告"):
                st.markdown(pricing.REPORT_PATH.read_text(encoding="utf-8"))
    hist_code = st.selectbox("价格历史", [m["code"] for m in mats], key="hist")
    hist = MAT.price_history(hist_code)
    if hist:
        st.dataframe(pd.DataFrame(hist)[["id", "price_type", "price", "price_date", "source", "supplier", "url", "created_at"]], hide_index=True, **ui.WIDE)
        del_id = st.selectbox("撤销某条记录（登记错了用）", [h["id"] for h in hist], format_func=lambda i: next(f"#{h['id']} {h['price_type']} {h['price']:.0f}（{h['created_at'][:16]}）" for h in hist if h["id"] == i))
        if st.button("撤销并联动重算"):
            res = pricing.delete_price(int(del_id))
            st.success(f"已撤销；现配方成本 {res['base_cost']:.0f}，{res['n_flagged']} 个配方明显变化")
            st.rerun()

with tab_f:
    forms = L.list_formulations()
    base_cost = COST.compute_simple(C.base_formulation())
    fdf = pd.DataFrame([{"编号": f["code"], "结构": f["structure"], "配方": f["formula"],
                         "当前成本": round(f["cost_used"]) if f.get("cost_used") else None, "口径": f.get("cost_tier"),
                         "估计价成本": round(f["cost_estimate"]) if f["cost_estimate"] else None,
                         "实价成本": round(f["cost_actual"]) if f["cost_actual"] else None,
                         "降本": round(base_cost - f["cost_used"]) if base_cost and f.get("cost_used") else None,
                         "面积指数": f["area_index"], "四项预期": (f["predicted"] or {}).get("effects_short"), "优先级": f["priority"],
                         "状态": f["status"], "思路": f["rationale"], "风险": f["risks"], "来源": f["origin"]} for f in forms])
    st.dataframe(fdf, hide_index=True, height=480, **ui.WIDE)
    s1, s2, s3 = ui.hrow(3)
    code = s1.selectbox("改状态", [f["code"] for f in forms]) if forms else None
    status = s2.selectbox("状态", ["候选", "推荐", "试验中", "过关", "淘汰"])
    if s3.button("更新", disabled=not code):
        L.set_status(code, status)
        st.rerun()
    top_path = KB["formulation"] / "Top20候选配方_初步预计.xlsx"
    if top_path.exists():
        st.download_button("下载 Top 20 表", top_path.read_bytes(), file_name=top_path.name)

with tab_g:
    st.caption("在约束内生成候选并核算成本；可选四项性能定性评估与排序。")
    c1, c3 = st.columns(2)
    n = c1.slider("生成数量", 5, 60, 20)
    themes = c3.multiselect("限定思路", list(G.THEMES))
    if st.button("生成候选"):
        cands = G.generate(n_out=n, themes=themes or None)
        st.session_state["cands"] = cands
    cands = st.session_state.get("cands") or []
    if cands:
        st.dataframe(pd.DataFrame([{"思路": c.theme, "配方": c.text(), "成本": c.cost, "口径": c.cost_tier, "降本%": round(c.savings_pct or 0, 1),
                                    "密度": c.density, "面积指数": c.area_index} for c in cands]), hide_index=True, **ui.WIDE)
        if st.button("定性评估并排序", disabled=not settings.llm_ready):
            with st.spinner("评估中…"):
                ranked = Q.rank(cands, Q.assess(cands), top_n=20)
            st.session_state["ranked"] = ranked
        ranked = st.session_state.get("ranked")
        if ranked:
            st.dataframe(X.top20_dataframe(ranked), hide_index=True, **ui.WIDE)
            if st.button("导出 Top 20 与询价清单"):
                p1 = X.export_top20(ranked)
                p2 = X.inquiry_list(ranked)
                st.success(f"已导出：{p1.name}、{p2.name}")
    with st.container(border=True):
        st.markdown("##### 手工核算")
        st.caption("格式如 LL 45, LD 10, HD 5, R1 39, AD 1")
        txt = st.text_input("配方", key="manual")
        if txt:
            try:
                comps = {}
                for part in txt.replace("/", ",").split(","):
                    k, v = part.split()
                    comps[k.strip()] = float(v)
                cd = G.evaluate(comps)
                st.write({"配方": cd.text(), "成本": cd.cost, "口径": cd.cost_tier, "降本%": round(cd.savings_pct or 0, 1), "密度": cd.density,
                          "面积指数": cd.area_index, "约束问题": cd.issues or "无"})
            except Exception as e:  # noqa: BLE001
                st.error(f"格式错误：{e}")

with tab_c:
    import altair as alt

    from polysage.pipeline import task as TASK

    c = C.load()
    t = TASK.load()
    mats_all = {m["code"]: m for m in MAT.list_materials(active_only=False)}
    base_f = C.base_formulation(c)

    # ---- 现配方 ----
    st.markdown("##### 现配方（重量 %）")
    cols_b = st.columns(6)
    new_base: dict[str, float] = {}
    codes_b = list(base_f) + [k for k in ("LLC", "RL") if k not in base_f]
    for i_, code in enumerate(codes_b):
        new_base[code] = cols_b[i_ % 6].number_input(f"{code} {mats_all.get(code, {}).get('name', '')[:12]}", 0.0, 100.0, float(base_f.get(code, 0.0)),
                                                     step=1.0, key=f"basef_{code}")
    total_b = sum(new_base.values())
    base_cost_now = COST.compute_simple({k: v for k, v in new_base.items() if v})
    st.caption(f"合计 {total_b:g}%　按当前价格卡成本 {base_cost_now:,.0f} 元/吨" if base_cost_now else f"合计 {total_b:g}%")

    # ---- 分组规则：可视化 + 可编辑 ----
    st.markdown("##### 分组用量规则")
    st.caption("横条为允许区间，圆点为现配方实际值。")
    groups = c.get("groups", {})
    rule_rows = []
    for r in c.get("rules", []):
        members = groups.get(r["group"], [])
        cur_val = sum(new_base.get(k, 0.0) for k in members)
        applies = r.get("applies") or "全部"
        active = applies != "core"
        rule_rows.append({"规则": r["name"], "组": r["group"], "成员": " ".join(members), "下限%": r.get("min"), "上限%": r.get("max"),
                          "适用": {"mono": "单层", "core": "三层芯层"}.get(applies, "全部"),
                          "现配方值%": round(cur_val, 1), "状态": ("—" if not active else ("✅" if (r.get("min", -1) <= cur_val <= r.get("max", 1e9)) else "⚠ 超出")),
                          "理由": r.get("reason", "")})
    rdf = pd.DataFrame(rule_rows)
    chart_df = rdf[rdf["状态"] != "—"].copy()
    chart_df["lo"] = chart_df["下限%"].fillna(0)
    chart_df["hi"] = chart_df["上限%"].fillna(100)
    bars = alt.Chart(chart_df).mark_bar(color="#cfd8e3", height=14).encode(
        x=alt.X("lo:Q", title="重量 %", scale=alt.Scale(domain=[0, 100])), x2="hi:Q", y=alt.Y("规则:N", sort=None, title=None))
    pts = alt.Chart(chart_df).mark_point(filled=True, size=90).encode(
        x="现配方值%:Q", y=alt.Y("规则:N", sort=None), color=alt.condition(alt.datum.状态 == "✅", alt.value("#2e7d32"), alt.value("#c62828")),
        tooltip=["规则", "下限%", "上限%", "现配方值%", "理由"])
    st.altair_chart((bars + pts).properties(height=28 * max(1, len(chart_df))), use_container_width=True)
    edited_rules = st.data_editor(rdf[["规则", "适用", "下限%", "上限%", "理由", "成员", "现配方值%", "状态"]], hide_index=True, key="rules_editor",
                                  disabled=["规则", "适用", "成员", "现配方值%", "状态"], **ui.WIDE)

    # ---- 单组分范围 ----
    with ui.expander("单组分范围（生成候选时每种料的上下限）"):
        bounds = c.get("component_bounds", {})
        bdf = pd.DataFrame([{"代码": k, "材料": mats_all.get(k, {}).get("name", ""), "下限%": v[0], "上限%": v[1]} for k, v in bounds.items()])
        edited_bounds = st.data_editor(bdf, hide_index=True, key="bounds_editor", disabled=["代码", "材料"], **ui.WIDE)

    # ---- 成本上限 ----
    st.markdown("##### 成本上限")
    m1, m2, m3 = st.columns(3)
    auto_limit = m1.toggle("上限 = 现配方成本（自动跟随价格）", value=bool(c.get("cost_limit_auto", True)))
    manual_limit = m2.number_input("手动上限 元/吨", 0, 50000, int(c.get("cost_limit") or 0), disabled=auto_limit)
    m3.caption(f"当前上限 {c.get('cost_limit'):,}，目标 {c.get('cost_target'):,}（目标 = 上限 × 0.95）")

    if st.button("保存约束", type="primary"):
        # 规则
        rules = c.get("rules", [])
        for i_, r in enumerate(rules):
            row = edited_rules.iloc[i_]
            for key, col in (("min", "下限%"), ("max", "上限%")):
                v = row[col]
                if pd.isna(v) or v == "":
                    r.pop(key, None)
                else:
                    r[key] = float(v)
            r["reason"] = str(row["理由"] or "")
        c["rules"] = rules
        # 单组分范围
        for _, row in edited_bounds.iterrows():
            c["component_bounds"][row["代码"]] = [float(row["下限%"]), float(row["上限%"])]
        # 现配方
        if abs(total_b - 100) > 0.5:
            st.error(f"现配方合计 {total_b:g}% ≠ 100%，未保存")
            st.stop()
        c["base_formulation"] = {k: v for k, v in new_base.items() if v}
        t["current_formulation"] = {k: {"pct": v, "grade": (t.get("current_formulation", {}).get(k, {}) or {}).get("grade", mats_all.get(k, {}).get("grade", ""))}
                                    for k, v in c["base_formulation"].items()}
        TASK.save(t)
        # 成本上限
        if auto_limit:
            c["cost_limit"], c["cost_target"] = "auto", "auto"
            c.pop("cost_limit_auto", None)
            c.pop("cost_target_auto", None)
        else:
            c["cost_limit"] = int(manual_limit)
            c["cost_target"] = int(manual_limit * 0.95)
            c.pop("cost_limit_auto", None)
            c.pop("cost_target_auto", None)
        C.save(c)
        st.success("已保存；重新出方案生效")
        st.rerun()
    with ui.expander("直接编辑 YAML"):
        import yaml

        txt = st.text_area("constraints.yaml", value=yaml.safe_dump(c, allow_unicode=True, sort_keys=False), height=300)
        if st.button("按 YAML 保存"):
            C.save(yaml.safe_load(txt))
            st.success("已保存")
            st.rerun()
