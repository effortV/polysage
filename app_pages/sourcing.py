from __future__ import annotations

import pandas as pd
import streamlit as st

from datetime import date

from polysage import activity, daily_quotes, db, forecast, jobs, sourcing, ui
from polysage.formulation import materials as MAT
from polysage.formulation import materials as MAT

ui.page_header("供应商寻源", "为每种原料找厂商与报价，和价格卡比较，生成询价清单；询价核实后一键登记为实价。")
MAT.seed_materials()

mats = MAT.list_materials(active_only=True)
names = {m["code"]: m["name"] for m in mats}
tab_daily, tab_run, tab_fc, tab_res, tab_rfq, tab_sup = st.tabs(["贸易商日报", "网查对照", "价格预判", "结果", "询价清单", "厂商库"], key="sourcing-workspace-tab", on_change="rerun")


def _fmt_price(v):
    return "-" if v is None or pd.isna(v) else f"{v:,.0f}"


# ---------------- 贸易商日报 ----------------
def _render_tab_daily() -> None:
    st.caption("把微信里的报价文字粘进来，或上传截图；解析后按“每类树脂当日最低到厂价”更新价格卡（到厂价 = 含税价 + 运费表）。")
    c1, c2 = st.columns([2, 1])
    trader = c1.text_input("报价商", value=st.session_state.get("dq_trader", ""), placeholder="例如：XX塑化 张经理")
    qdate = c2.date_input("报价日期", value=date.today())
    text = st.text_area("报价文字（微信原文直接粘贴）", height=180, placeholder="线性：\n浙石化7042，本周计划，9650H；\n宝来7042，本周计划，9580H；\n高压：\n浙石化2426H，下周计划，12000H；")
    imgs = st.file_uploader("或上传报价截图（可多张）", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
    b1, b2 = st.columns([1, 3])
    if b1.button("解析", type="primary", disabled=not trader.strip() or (not text.strip() and not imgs)):
        st.session_state["dq_trader"] = trader.strip()
        full = text
        for f in imgs or []:
            try:
                ocr = daily_quotes.ocr_image(f.getvalue())
                full += "\n" + ocr
            except Exception as e:  # noqa: BLE001
                st.error(f"截图 {f.name} 识别失败：{e}")
        with st.spinner("解析中…"):
            try:
                rows = daily_quotes.parse_text(full, trader.strip(), qdate.isoformat())
            except Exception as e:  # noqa: BLE001
                st.error(f"解析失败：{e}")
                rows = []
        st.session_state["dq_rows"] = rows
        if not rows:
            st.warning("没有解析出报价，检查原文或截图。")
    rows = st.session_state.get("dq_rows") or []
    if rows:
        st.markdown(f"解析出 **{len(rows)}** 条，可以直接改再入库：")
        df = pd.DataFrame([{"类别": r["family"], "厂家": r["producer"], "牌号": r["grade"], "仓库地": r["warehouse"], "货物状态": r["delivery"],
                            "含税价": r["price"], "含税": r["tax_included"], "运费": r["freight"], "到厂价": r["landed"], "备注": r["note"]} for r in rows])
        edited = st.data_editor(df, hide_index=True, key="dq_editor", num_rows="dynamic",
                                column_config={"类别": st.column_config.SelectboxColumn(options=daily_quotes.FAMILIES),
                                               "含税价": st.column_config.NumberColumn(format="%.0f"), "运费": st.column_config.NumberColumn(format="%.0f"),
                                               "到厂价": st.column_config.NumberColumn(format="%.0f", disabled=True)}, **ui.WIDE)
        auto = st.checkbox("入库后按每类最低到厂价更新价格卡（实价）", value=True)
        if st.button("入库", type="primary", key="dq_save"):
            final = []
            for _, r in edited.iterrows():
                if not r["含税价"] or pd.isna(r["含税价"]):
                    continue
                price = float(r["含税价"]); fr = float(r["运费"]) if not pd.isna(r["运费"]) else daily_quotes.freight_for(str(r["仓库地"] or ""))
                final.append({"family": r["类别"], "producer": str(r["厂家"] or ""), "grade": str(r["牌号"] or ""), "warehouse": str(r["仓库地"] or ""),
                              "delivery": str(r["货物状态"] or ""), "price": price, "tax_included": bool(r["含税"]), "note": str(r["备注"] or ""),
                              "freight": fr, "landed": price + fr, "trader": trader.strip() or st.session_state.get("dq_trader", "贸易商"),
                              "date": qdate.isoformat()})
            saved = daily_quotes.save_rows(final)
            applied = daily_quotes.apply_cheapest(qdate.isoformat()) if auto else []
            st.session_state["dq_rows"] = []
            msg = f"已入库：新增 {saved['new']}，重复 {saved['dup']}，跳过 {saved['skip']}（“其他”类不进价格卡）。"
            if applied:
                msg += " 价格卡已更新：" + "；".join(f"{a['code']} → {a['landed']:.0f}（{a['producer']} {a['grade']} @ {a['warehouse']}）" for a in applied)
            st.success(msg)
            st.rerun()

    ps = daily_quotes.picks()
    if ps:
        st.subheader(f"今日建议（{next(iter(ps.values()))['date']}，每类最低到厂价）")
        cols = st.columns(len(ps))
        for col, (fam, p) in zip(cols, ps.items()):
            b = p["best"]
            delta = None if p["change"] is None else f"{p['change']:+.0f} vs 上次"
            col.metric(daily_quotes.FAMILY_LABEL[fam], f"{b['landed_price']:,.0f}", delta=delta, delta_color="inverse",
                       help=f"{b['grade']} @ {b['warehouse'] or '未注明'}（{b['delivery']}）含税 {b['price']:.0f}，{b['trader']}")
        table = []
        for fam, p in ps.items():
            for i, r in enumerate(p["top"], 1):
                table.append({"类别": daily_quotes.FAMILY_LABEL[fam], "名次": i, "牌号": r["grade"], "仓库地": r["warehouse"],
                              "货物状态": r["delivery"], "含税价": r["price"], "到厂价": r["landed_price"], "报价商": r["trader"],
                              "价格卡现价": p.get("current_card")})
        st.dataframe(pd.DataFrame(table), hide_index=True, **ui.WIDE)
        if st.button("按最低价更新价格卡", key="dq_apply"):
            applied = daily_quotes.apply_cheapest()
            st.success("已更新：" + "；".join(f"{a['code']} → {a['landed']:.0f}" for a in applied) if applied else "价格卡已是当日最低价，无需更新。")
        fams = [f for f in ps if daily_quotes.history(f)]
        if fams:
            with ui.expander("价格趋势（每日最低到厂价）"):
                hist = {}
                for f in fams:
                    for h in daily_quotes.history(f):
                        hist.setdefault(h["d"], {})[daily_quotes.FAMILY_LABEL[f]] = h["landed"]
                st.line_chart(pd.DataFrame.from_dict(hist, orient="index").sort_index())
    with ui.expander("网查对照（同口径，近两周）"):
        _render_web_compare([f for f in daily_quotes.FAMILIES if f != "其他"])
    with ui.expander("运费表（仓库地 → 到厂，元/吨；按子串匹配，改完保存）"):
        fdf = pd.DataFrame([{"仓库地": k, "运费": v} for k, v in daily_quotes.load_freight().items()])
        fedit = st.data_editor(fdf, hide_index=True, num_rows="dynamic", key="freight_editor", **ui.WIDE)
        if st.button("保存运费表"):
            daily_quotes.save_freight({str(r["仓库地"]): float(r["运费"]) for _, r in fedit.iterrows() if str(r["仓库地"]).strip() and not pd.isna(r["运费"])})
            st.success("已保存；下次解析按新运费算到厂价。")


# ---------------- 价格预判 ----------------
def _render_tab_forecast() -> None:
    st.caption("期货（大商所 L 线性主连）+ 我们的现货报价历史 + 近一周行情评述 → 每类 1 周 / 1 月预期；"
               "再把最近一次推荐的方案按预期价格重算成本，看哪些方案涨价也扛得住。只是参考，不是交易建议。")
    c1, c2 = st.columns([1, 3])
    if c1.button("更新预判（约 2～3 分钟）", type="primary", key="fc_run"):
        with st.spinner("抓期货、找评述、模型判断…"):
            try:
                forecast.run()
                st.success("已更新")
            except Exception as e:  # noqa: BLE001
                st.error(f"更新失败：{e}")
        st.rerun()
    lt = forecast.latest()
    if not lt:
        st.info("还没有预判，点“更新预判”。")
        return
    c2.caption(f"最近更新：{next(iter(lt.values()))['at'][:16].replace('T', ' ')}")
    cols = st.columns(len(lt))
    for col, (fam, r) in zip(cols, lt.items()):
        cur = r.get("current")
        col.metric(forecast.FAMILY_LABEL[fam], f"{cur:,.0f}" if cur else "-",
                   delta=(f"1 月 {r['pct_1m']:+.1f}% → {r['price_1m']:,.0f}" if r.get("price_1m") else None), delta_color="inverse",
                   help=f"1 周 {r['pct_1w']:+.1f}% → {r.get('price_1w') or '-'}；方向 {r['direction']}；置信 {r['confidence']:.0%}")
    for fam, r in lt.items():
        with ui.expander(f"{forecast.FAMILY_LABEL[fam]}：{r['direction']}，1 周 {r['pct_1w']:+.1f}%，1 月 {r['pct_1m']:+.1f}%，置信 {r['confidence']:.0%}"):
            m = r.get("method") or {}
            st.caption(f"方法：{m.get('blend', '')}；模型给 1 月 {m.get('llm_pct_1m', 0):+.1f}%，期货隐含 "
                       + (f"{m['futures_implied_1m']:+.1f}%" if m.get("futures_implied_1m") is not None else "不可用"))
            for d in r.get("drivers") or []:
                st.markdown(f"- {d.get('text')}" + (f"（[来源]({d['url']})）" if d.get("url") else ""))
            if r.get("sources"):
                st.markdown("评述来源：" + "；".join(f"[{x.get('title') or x.get('url')}]({x.get('url')})" for x in r["sources"] if x.get("url")))
    try:
        fs = forecast.futures_signal()
        if fs.get("ok"):
            st.markdown(f"**大商所 L 主连**：{fs['last']:.0f}（{fs['date']}），5 日 {fs['chg_5d']:+.1f}%，20 日 {fs['chg_20d']:+.1f}%，"
                        f"MA5 {fs['ma5']} / MA20 {fs['ma20']}" + (f"，远月-近月 {fs['term_pct']:+.1f}%（{', '.join(f'{k} {v:.0f}' for k, v in fs['curve'].items())}）" if fs.get("term_pct") is not None else ""))
            st.line_chart(pd.DataFrame(fs["series"], columns=["日期", "L 主连收盘"]).set_index("日期"))
    except Exception as e:  # noqa: BLE001
        st.caption(f"期货数据暂不可用：{e}")
    rows = forecast.scheme_impact(top_n=12)
    if rows:
        st.subheader("预期价格（1 月）下的方案成本")
        st.dataframe(pd.DataFrame([{"方案": r["name"], "主题": r["theme"], "现成本": r["cost_now"], "预期成本": r["cost_future"],
                                    "变化": r["delta"], "变化%": r["delta_pct"]} for r in rows]), hide_index=True, **ui.WIDE)
        st.caption("变化% 越小的方案对涨价越不敏感；配方推荐助手已经能看到这份预判。")


# ---------------- 网查对照 ----------------
def _render_tab_run() -> None:
    st.caption("按贸易商日报同样的口径（厂家 + 牌号 + 仓库地 + 货物状态 + 含税价）到行情站/报价页网查近两周的报价，只作对照，不进价格卡。"
               "检索目标取自最近日报里的厂家+牌号（没有日报时用默认牌号表）。")
    fams = [f for f in daily_quotes.FAMILIES if f != "其他"]
    c1, c2 = st.columns([3, 1])
    pick = c1.multiselect("类别", fams, default=["LLDPE", "LDPE", "HDPE"], format_func=lambda f: daily_quotes.FAMILY_LABEL[f])
    depth = c2.selectbox("深度", ["快", "标准", "深"], index=1, help="每类检索词条数：快 3、标准 6、深 10；每条取 3 个页面")
    with ui.expander("本次会用的检索词"):
        for f in pick:
            st.markdown(f"**{daily_quotes.FAMILY_LABEL[f]}**：" + "；".join(daily_quotes.web_queries(f, depth)))
    running = jobs.is_running()
    if st.button("开始网查", type="primary", disabled=running or not pick):
        if jobs.start_sourcing(pick, depth=depth):
            st.rerun()
        else:
            st.warning("已有后台任务在运行，等它结束或在侧栏删除后再试。")
    if running and jobs.current() == "sourcing":
        st.info(f"网查进行中（侧栏可暂停/删除）· 已运行 {activity.fmt_seconds(jobs.info()['elapsed'])}")

    @st.fragment(run_every="3s" if running else None)
    def _log_panel() -> None:
        with ui.expander("运行日志", expanded=jobs.is_running()):
            st.code("\n".join(sourcing.read_log(40)) or "（空）", language="text")

    _log_panel()
    lr = sourcing.last_run()
    if lr:
        st.caption(f"上次网查：{lr['at'][:16].replace('T', ' ')} · {', '.join(lr['codes'])}")
    _render_web_compare(fams)

    st.divider()
    st.subheader("辅料与再生料")
    st.caption("再生料（R1/RL/R2）、助剂母料（AD）、填充母料（FL）、POE/EVA 等不在贸易商日报里，按材料代码网查厂商与报价（近一个月），"
               "可把最低到厂价写成估计价；询价核实后到「结果」里登记为实价。")
    codes_all = [c for c in daily_quotes.MATERIAL_QUERIES if MAT.get_material(c)]
    default_codes = [c for c in ("R1", "RL", "AD") if c in codes_all]
    mc1, mc2, mc3 = st.columns([3, 1, 1])
    pick_codes = mc1.multiselect("材料", codes_all, default=default_codes,
                                 format_func=lambda c: f"{c} · {(MAT.get_material(c) or {}).get('name', c)}")
    mdepth = mc2.selectbox("深度", ["快", "标准", "深"], index=0, key="mat_depth")
    apply_est = mc3.checkbox("写成估计价", value=True, help="把每种料的最低到厂价写进价格卡的估计价（不是实价）")
    if st.button("网查辅料报价", disabled=jobs.is_running() or not pick_codes, key="mat_web"):
        with st.spinner("检索与抽取中（每种料约 1 分钟）…"):
            try:
                summary = daily_quotes.web_material_quotes(pick_codes, depth=mdepth, pages_per_query=2, force=True)
                applied = daily_quotes.apply_web_estimates(pick_codes) if apply_est else []
                msg = "；".join(f"{c} {s['rows']} 条（新增 {s['new']}）" if s["rows"]
                                else f"{c} 查不到公开报价" + ("（这类料只能直接问厂家）" if c in daily_quotes.HARD_TO_SOURCE else "")
                                for c, s in summary.items())
                if applied:
                    msg += " | 估计价已更新：" + "，".join(f"{a['code']} → {a['landed']:.0f}" for a in applied)
                st.success(msg)
            except Exception as e:  # noqa: BLE001
                st.error(f"网查失败：{e}")
        st.rerun()
    mrows = []
    for c in codes_all:
        for i, r in enumerate(daily_quotes.material_rows(c, top=5), 1):
            cur = MAT.current_prices().get(c, {})
            cur_p = cur.get("actual") if cur.get("actual") is not None else cur.get("estimate")
            mrows.append({"代码": c, "材料": (MAT.get_material(c) or {}).get("name", c), "名次": i, "供应商": r["supplier"],
                          "类型": r["kind"], "地区": r["region"], "规格": r["grade"], "报价": r["price"], "到厂价": r["landed_price"],
                          "价格卡": cur_p, "起订量": r["moq"], "联系方式": r["contact"], "日期": r["quote_date"], "来源": r["source_url"]})
    if mrows:
        st.dataframe(pd.DataFrame(mrows), hide_index=True, column_config={"来源": st.column_config.LinkColumn()}, **ui.WIDE)
    else:
        st.info("辅料还没有网查报价；选好材料点“网查辅料报价”。")


def _render_web_compare(fams: list[str]) -> None:
    rows = []
    for f in fams:
        for i, r in enumerate(daily_quotes.web_rows(f), 1):
            rows.append({"类别": daily_quotes.FAMILY_LABEL[f], "名次": i, "牌号": r["grade"], "仓库地": r["warehouse"], "货物状态": r["delivery"],
                         "含税价": r["price"], "到厂价": r["landed_price"], "报价方": r["trader"], "日期": r["quote_date"], "来源": r["source_url"]})
    if rows:
        st.subheader("网查对照（近两周，每类前 5）")
        st.dataframe(pd.DataFrame(rows), hide_index=True, column_config={"来源": st.column_config.LinkColumn()}, **ui.WIDE)
    else:
        st.info("还没有网查报价；点“开始网查”。")

# ---------------- 结果 ----------------
def _render_tab_res() -> None:
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
def _render_tab_rfq() -> None:
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
def _render_tab_sup() -> None:
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


_TAB_RENDERERS = (
    (tab_daily, _render_tab_daily),
    (tab_run, _render_tab_run),
    (tab_fc, _render_tab_forecast),
    (tab_res, _render_tab_res),
    (tab_rfq, _render_tab_rfq),
    (tab_sup, _render_tab_sup),
)
for _tab, _renderer in _TAB_RENDERERS:
    with _tab:
        if _tab.open:
            _renderer()
