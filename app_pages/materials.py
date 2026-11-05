from __future__ import annotations

import pandas as pd
import streamlit as st

from polysage import db, jobs, pricing, ui
from polysage.config import settings
from polysage.formulation import library as L
from polysage.formulation import materials as MAT

ui.page_header("原料与价格", "原料库、价格卡与联动、配方库、候选生成、约束。")
MAT.seed_materials()
L.seed_top20()

# 旧的单层“生成与评估”“约束”页已由「配方推荐」里的分档寻优、评估一个配方、系数表取代
tab_m, tab_p, tab_f = st.tabs(["原料库", "价格卡与回填", "配方库"], key="materials-workspace-tab", on_change="rerun")

def _render_tab_m() -> None:
    mats = MAT.list_materials(active_only=False)
    st.caption("原料库只预置现配方在用的 4 种料（LL / LD / HD / R1）；其余都由智能体上网找，每条带来源网址。"
               "物性以供应商 TDS 为准。")
    with ui.expander("智能体找新料", expanded=not any(m.get("source_url") for m in mats)):
        g1, g2, g3, g4 = st.columns([4, 1, 1, 1])
        goal = g1.text_input("想找什么", placeholder="留空按默认降本方向找：同类更便宜的 LLDPE / 再生料 / 增韧料 / 助剂",
                             key="scout_goal")
        sdepth = g2.selectbox("深度", ["快", "标准", "深"], index=0, key="scout_depth")
        smax = g3.number_input("最多新增", 1, 20, 6, key="scout_max")
        if g4.button("开始找", disabled=jobs.is_running(), key="scout_go"):
            from polysage import scout

            log = st.empty()
            seen: list[str] = []
            with st.spinner("上网找料中（一个方向约 1～2 分钟）…"):
                try:
                    out = scout.discover(goal, depth=sdepth, max_new=int(smax),
                                         echo=lambda t: (seen.append(t), log.code("\n".join(seen[-8:])))[0])
                    if out["added"]:
                        st.success(f"新增 {len(out['added'])} 种料：" + "、".join(f"{a['code']} {a['name']}" for a in out["added"]))
                    else:
                        st.warning("这次没找到能入库的新料（页面里没有具体牌号或报价）。换个说法再试，或直接去「供应商与报价」按材料查。")
                    for n in out["notes"][:5]:
                        st.caption(n)
                except Exception as e:  # noqa: BLE001
                    st.error(f"找料失败：{e}")
            st.rerun()
        found = [m for m in mats if m.get("source_url")]
        if found:
            st.dataframe(pd.DataFrame([{"代码": m["code"], "材料": m["name"], "牌号": m["grade"], "类别": m["category"],
                                        "怎么来的": m.get("origin"), "发现时间": (m.get("discovered_at") or "")[:16],
                                        "来源": m.get("source_url")} for m in found]),
                         hide_index=True, column_config={"来源": st.column_config.LinkColumn("来源", width="medium")}, **ui.WIDE)
        else:
            st.caption("还没有智能体发现的料。")
        if st.button("清理早期假设料（内部技术路线附录 D）", key="purge_assumed"):
            from polysage.formulation import library as L

            res = MAT.purge_assumed()
            pur = L.purge_unbuyable()
            st.success(f"删掉 {res['prices_deleted']} 条假设价、{len(res['removed'])} 种没有依据的料"
                       + (f"（{'、'.join(res['removed'])}）" if res["removed"] else "")
                       + f"；连带清掉 {pur if isinstance(pur, int) else len(pur)} 条用到它们的配方。")
            st.rerun()
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

def _render_tab_p() -> None:
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
            st.success(f"已登记并联动：现配方成本 {pricing.cost_text(res['base_cost'])}，{res['n_flagged']} 个配方成本/排名明显变化（见价格变动报告）")
            st.rerun()
    with st.container(border=True):
        st.markdown("##### 询价清单回填")
        st.caption("上传填好的《询价清单.xlsx》，读取“代码 / 到厂含税价 / 供应商 / 起订量 / 报价日期”。")
        up = st.file_uploader("询价清单", type=["xlsx", "csv"], key="inq")
        if up is not None and st.button("导入实价"):
            df_in = pd.read_excel(up) if up.name.endswith("xlsx") else pd.read_csv(up)
            res = pricing.import_actual_prices(df_in.to_dict("records"))
            st.success(f"导入 {res['n_imported']} 条实价并联动：现配方成本 {pricing.cost_text(res['base_cost'])}，{res['n_flagged']} 个配方明显变化；到“智能体”页重新出方案")
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

def _parse_layer(txt: str) -> dict[str, float]:
    comps: dict[str, float] = {}
    for part in (txt or "").replace("，", "/").replace(",", "/").split("/"):
        if part.strip():
            k, v = part.split()
            comps[k.strip()] = float(v)
    return comps


def _render_tab_f() -> None:
    from polysage.aba import report as RP
    from polysage.aba.model import Ctx, Design, base_design

    forms = L.list_formulations()
    ctx = Ctx()
    ref = base_design()
    ref_cost = ctx.cost(ref)
    st.caption("配方来源：配方推荐（分档寻优、对话）、研发流水线 ④、这里手工录入。相同组分不会重复入库。"
               "ABA 配方按层显示；整膜组分只用来算成本和采购。")
    with ui.expander("手工录入 ABA 配方"):
        with st.form("add_formulation"):
            c1, c2 = st.columns(2)
            a_txt = c1.text_input("A 层（表层）组分，合计 100", placeholder="LL 66.7 / R1 33.3")
            b_txt = c2.text_input("B 层（芯层）组分，合计 100", placeholder="LL 53.4 / R1 26.6 / FL 20")
            c3, c4, c5 = st.columns(3)
            a_ratio = c3.number_input("A 层占比", 0.2, 0.6, 0.45, step=0.05)
            thick = c4.number_input("厚度（丝）", 2.0, 8.0, 4.5, step=0.5)
            rationale = c5.text_input("思路 / 备注")
            if st.form_submit_button("存入配方库"):
                try:
                    d = Design(a=_parse_layer(a_txt), b=_parse_layer(b_txt), a_ratio=a_ratio, thickness=thick)
                except ValueError:
                    st.error("格式不对，例如：LL 66.7 / R1 33.3")
                    d = None
                if d:
                    unknown = [k for k in list(d.a) + list(d.b) if not MAT.get_material(k)]
                    issues = ctx.check(d)
                    if unknown:
                        st.error(f"原料库没有代码：{', '.join(unknown)}（先到「原料库」登记）")
                    elif issues:
                        st.error("；".join(issues))
                    else:
                        ev = ctx.evaluate(d, ref)
                        ev["drivers"] = ctx.drivers(d, ref)
                        if rationale:
                            ev["drivers"] = [rationale] + ev["drivers"]
                        out = {"groups": {"手工": {ev["grade"]: [ev]}}, "scenario": {"label": "当前价"}}
                        codes = RP.save_to_library(out, origin="手工录入")
                        if codes:
                            st.success(f"已存入 {codes[0]}：{d.text()}")
                            st.rerun()
                        else:
                            st.error("未入库：配方里有买不到的料（先到「供应商与报价」网查报价，或在价格卡登记正式报价）。")
    show_old = st.toggle("显示旧版单层配方（四项指标时期）", value=False)
    if not show_old:
        forms = [f for f in forms if f.get("structure") == "ABA"]
    fdf = pd.DataFrame([{"编号": f["code"], "结构": f["structure"], "配方": f["formula"],
                         "当前成本": round(f["cost_used"]) if f.get("cost_used") else None, "口径": f.get("cost_tier"),
                         "较现配方": round(ref_cost - f["cost_used"]) if ref_cost and f.get("cost_used") else None,
                         "三项预期": (f["predicted"] or {}).get("effects_short"),
                         "把握": (f["predicted"] or {}).get("pass_confidence"), "分档": f["priority"],
                         "状态": f["status"], "依据": f["rationale"], "来源": f["origin"],
                         "原料采购（怎么来的）": f.get("sourcing_note") or "", "全部可采购": "是" if f.get("buyable") else "否"}
                        for f in forms])
    if fdf.empty:
        st.caption("还没有 ABA 配方：到「配方推荐 › 分档最低成本」算一次，或在这里手工录入。")
    else:
        st.dataframe(fdf, hide_index=True, height=480, **ui.WIDE)
    s1, s2, s3 = ui.hrow(3)
    code = s1.selectbox("改状态", [f["code"] for f in forms]) if forms else None
    status = s2.selectbox("状态", ["候选", "推荐", "试验中", "过关", "淘汰"])
    if s3.button("更新", disabled=not code):
        L.set_status(code, status)
        st.rerun()


_TAB_RENDERERS = (
    (tab_m, _render_tab_m),
    (tab_p, _render_tab_p),
    (tab_f, _render_tab_f),
)
for _tab, _renderer in _TAB_RENDERERS:
    with _tab:
        if _tab.open:
            _renderer()
