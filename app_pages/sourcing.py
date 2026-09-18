from __future__ import annotations

import pandas as pd
import streamlit as st

from polysage import activity, db, jobs, sourcing, ui
from polysage.formulation import materials as MAT

ui.page_header("供应商寻源", "为每种原料找厂商与报价，和价格卡比较，生成询价清单；询价核实后一键登记为实价。")
MAT.seed_materials()

mats = MAT.list_materials(active_only=True)
names = {m["code"]: m["name"] for m in mats}
tab_run, tab_res, tab_rfq, tab_sup = st.tabs(["寻源", "结果", "询价清单", "厂商库"])


def _fmt_price(v):
    return "-" if v is None or pd.isna(v) else f"{v:,.0f}"


# ---------------- 寻源 ----------------
with tab_run:
    st.caption("网查到的是挂牌价 / 平台标价，口径常不一致；用来确定该向谁询价和大致区间，实价以询价为准。")
    default_codes = [m["code"] for m in mats if m.get("use_flag") != "默认不用"][:8]
    c1, c2 = st.columns([3, 1])
    codes = c1.multiselect("材料", options=[m["code"] for m in mats], default=default_codes,
                           format_func=lambda c: f"{c} · {names.get(c, c)}")
    depth = c2.selectbox("深度", list(sourcing.DEPTHS), index=1, help="每种材料的检索词条数：快 2、标准 4、深 6；每条取 3 个页面")
    c3, c4 = st.columns([3, 1])
    extra = c3.text_input("补充关键词（可选）", placeholder="例如：华东 现货 / 某牌号 / 某地区")
    only_prod = c4.checkbox("结果只看生产商/回收厂", value=False)
    running = jobs.is_running()
    if st.button("开始寻源", type="primary", disabled=running or not codes):
        if jobs.start_sourcing(codes, depth=depth, extra=extra, only_producers=only_prod):
            st.rerun()
        else:
            st.warning("已有后台任务在运行，等它结束或在侧栏删除后再试。")
    if running and jobs.current() == "sourcing":
        st.info(f"寻源进行中（侧栏可暂停/删除）· 已运行 {activity.fmt_seconds(jobs.info()['elapsed'])}")

    @st.fragment(run_every="3s" if running else None)
    def _log_panel() -> None:
        with ui.expander("运行日志", expanded=jobs.is_running()):
            st.code("\n".join(sourcing.read_log(40)) or "（空）", language="text")

    _log_panel()
    lr = sourcing.last_run()
    if lr:
        st.caption(f"上次寻源：{lr['at'][:16].replace('T', ' ')} · {', '.join(lr['codes'])}")

# ---------------- 结果 ----------------
with tab_res:
    all_quotes = sourcing.quotes(include_untrusted=True)
    if not all_quotes:
        st.info("还没有报价。到「寻源」运行一次，或在「厂商库」手动录入。")
    else:
        have = [c for c in names if any(q["material_code"] == c for q in all_quotes)]
        pick = st.selectbox("材料", have, format_func=lambda c: f"{c} · {names[c]}")
        s = sourcing._compare_summary(pick)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("现价（元/吨）", _fmt_price(s.get("current")))
        m2.metric("最低网查报价", _fmt_price(s.get("min_price")), help=s.get("best_supplier") or "")
        m3.metric("比现价", f"低 {s['saving_pct']:.1f}%" if s.get("saving_pct") and s["saving_pct"] > 0 else
                  (f"高 {-s['saving_pct']:.1f}%" if s.get("saving_pct") is not None else "-"))
        m4.metric("有报价的厂商", s.get("n_suppliers", 0))
        st.caption(sourcing.summary_text(pick))
        rows = sourcing.compare(pick)
        rows += [q for q in all_quotes if q["material_code"] == pick and q["status"] == "不可信"]
        df = pd.DataFrame([{
            "id": r["id"], "加入询价": bool(r.get("in_rfq")), "不可信": r["status"] == "不可信", "厂商": r["supplier"], "类型": r["kind"],
            "地区": r["region"], "牌号/规格": r["grade"], "报价": r["price"], "口径": r["basis"], "口径存疑": bool(r.get("basis_flag")),
            "与现价": (f"{r['diff']:+.0f}（{r['diff_pct']:+.1f}%）" if r.get("diff") is not None else "-"),
            "起订量": r["moq"], "日期": r["quote_date"], "联系方式": r["contact"], "可信度": r["credibility"], "状态": r["status"],
            "证据": r["evidence"], "来源": r["source_url"]} for r in rows])
        edited = st.data_editor(
            df, hide_index=True, height=min(80 + 36 * len(df), 560), key=f"quotes_{pick}",
            column_config={"id": None, "报价": st.column_config.NumberColumn(format="%.0f"),
                           "来源": st.column_config.LinkColumn(), "加入询价": st.column_config.CheckboxColumn(),
                           "不可信": st.column_config.CheckboxColumn()},
            disabled=[c for c in df.columns if c not in ("加入询价", "不可信")], **ui.WIDE)
        if st.button("保存勾选", key=f"save_{pick}"):
            for _, r in edited.iterrows():
                orig = next((x for x in rows if x["id"] == r["id"]), None)
                if orig is None:
                    continue
                if bool(r["加入询价"]) != bool(orig.get("in_rfq")):
                    sourcing.set_quote(int(r["id"]), in_rfq=int(bool(r["加入询价"])),
                                       status="询价中" if r["加入询价"] and orig["status"] == "网查" else orig["status"])
                if bool(r["不可信"]) != (orig["status"] == "不可信"):
                    sourcing.set_quote(int(r["id"]), status="不可信" if r["不可信"] else "网查")
            st.success("已保存")
            st.rerun()
        if sourcing.REPORT_PATH.exists():
            with ui.expander("寻源报告（Markdown）"):
                st.markdown(sourcing.REPORT_PATH.read_text(encoding="utf-8"))

# ---------------- 询价清单 ----------------
with tab_rfq:
    rfq = sourcing.rfq_rows()
    if not rfq:
        st.info("询价清单为空：在「结果」里勾选“加入询价”。")
    else:
        st.caption("导出给采购打电话；回来把“实际报价”填上，点“登记为实价”，价格卡与推荐方案自动联动。")
        df = pd.DataFrame([{"id": q["id"], "材料": names.get(q["material_code"], q["material_code"]), "厂商": q["supplier"], "类型": q["kind"],
                            "地区": q["region"], "牌号/规格": q["grade"], "网查报价": q["price"], "口径": q["basis"], "起订量": q["moq"],
                            "联系方式": q["contact"], "实际报价": q.get("actual_price"), "状态": q["status"], "备注": q.get("note") or ""}
                           for q in rfq])
        edited = st.data_editor(df, hide_index=True, key="rfq_editor",
                                column_config={"id": None, "网查报价": st.column_config.NumberColumn(format="%.0f"),
                                               "实际报价": st.column_config.NumberColumn(format="%.0f", help="询价得到的含税到厂价（元/吨）")},
                                disabled=[c for c in df.columns if c not in ("实际报价", "备注")], **ui.WIDE)
        c1, c2, c3 = st.columns(3)
        if c1.button("登记为实价（已填实际报价的行）", type="primary"):
            n = 0
            for _, r in edited.iterrows():
                if r["实际报价"] is not None and not pd.isna(r["实际报价"]) and r["状态"] != "已登记":
                    sourcing.register_actual(int(r["id"]), float(r["实际报价"]), note=str(r["备注"] or ""))
                    n += 1
            st.success(f"已登记 {n} 条实价，推荐方案已按新价格重排。" if n else "没有可登记的行（先填实际报价）。")
            if n:
                st.rerun()
        if c2.button("保存备注"):
            for _, r in edited.iterrows():
                sourcing.set_quote(int(r["id"]), note=str(r["备注"] or ""))
            st.success("已保存")
        path = sourcing.export_rfq()
        c3.download_button("下载询价清单 xlsx", data=path.read_bytes(), file_name=path.name,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---------------- 厂商库 ----------------
with tab_sup:
    sups = sourcing.list_suppliers()
    st.caption("寻源累积的厂商档案；也可以把现有供应商录进来，便于对比。")
    with ui.expander("手动录入厂商 / 报价"):
        with st.form("add_supplier"):
            a1, a2, a3 = st.columns(3)
            s_name = a1.text_input("厂商名称")
            s_kind = a2.selectbox("类型", sourcing.KINDS, index=0)
            s_region = a3.text_input("地区")
            b1, b2, b3 = st.columns(3)
            s_contact = b1.text_input("联系方式")
            s_code = b2.selectbox("报价材料（可选）", [""] + [m["code"] for m in mats], format_func=lambda c: f"{c} · {names[c]}" if c else "（不填报价）")
            s_price = b3.number_input("报价（元/吨）", min_value=0.0, value=0.0, step=50.0)
            s_basis = st.text_input("口径 / 备注", placeholder="含税到厂 / 起订 1 吨 / 已合作")
            if st.form_submit_button("保存"):
                if not s_name.strip():
                    st.error("厂商名称不能为空")
                else:
                    sid = sourcing.upsert_supplier({"name": s_name, "kind": s_kind, "region": s_region, "contact": s_contact,
                                                    "notes": s_basis, "materials": s_code, "credibility": 5, "status": "已合作" if "已合作" in s_basis else "手动"})
                    if s_code and s_price > 0:
                        sourcing.add_quote(s_code, {"supplier": s_name, "kind": s_kind, "region": s_region, "price": s_price,
                                                    "basis": s_basis, "contact": s_contact, "grade": "", "moq": "", "date": "",
                                                    "evidence": "手动录入"}, source_url="手动录入", credibility=5)
                    st.success(f"已保存（#{sid}）")
                    st.rerun()
    if sups:
        df = pd.DataFrame([{"id": s["id"], "厂商": s["name"], "类型": s["kind"], "地区": s["region"], "联系方式": s["contact"],
                            "材料": s["materials"], "报价数": s["n_quotes"], "最低报价": s["min_price"], "可信度": s["credibility"],
                            "状态": s["status"], "备注": s["notes"], "更新": (s.get("updated_at") or "")[:10]} for s in sups])
        edited = st.data_editor(df, hide_index=True, key="sup_editor", column_config={"id": None},
                                disabled=["厂商", "报价数", "最低报价", "更新"], **ui.WIDE)
        if st.button("保存厂商库修改"):
            for _, r in edited.iterrows():
                db.update("suppliers", int(r["id"]), {"kind": r["类型"], "region": r["地区"], "contact": r["联系方式"],
                                                                "materials": r["材料"], "credibility": int(r["可信度"] or 3),
                                                                "status": r["状态"], "notes": r["备注"], "updated_at": db.now()})
            st.success("已保存")
        pick_s = st.selectbox("查看某厂商的报价", [s["id"] for s in sups], format_func=lambda i: next(s["name"] for s in sups if s["id"] == i))
        qs = [q for q in sourcing.quotes(include_untrusted=True) if q["supplier_id"] == pick_s]
        if qs:
            st.dataframe(pd.DataFrame([{"材料": names.get(q["material_code"], q["material_code"]), "牌号": q["grade"], "报价": q["price"],
                                        "口径": q["basis"], "日期": q["quote_date"], "状态": q["status"], "实际报价": q.get("actual_price"),
                                        "来源": q["source_url"]} for q in qs]), hide_index=True, **ui.WIDE)
    else:
        st.info("厂商库为空。")
