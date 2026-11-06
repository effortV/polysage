"""ABA 三层膜：成本、定性预测、分档寻优、价格场景、实验闭环、对话工具。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from polysage import db
from polysage.aba import experiments as E
from polysage.aba import optimize as O
from polysage.aba import prices as P
from polysage.aba import rules as R
from polysage.aba.model import Ctx, Design, base_design
from polysage.formulation import materials as MAT

PRICES = {"LL": 9670, "R1": 8250, "FL": 2500, "HD": 9300}


@pytest.fixture()
def aba_prices(home):
    """每个用例开头把现配方四种料的价格钉死（实价，最新一条生效），不受别的测试影响。"""
    MAT.seed_materials()
    marks = ",".join("?" for _ in PRICES)
    others = db.q(f"SELECT * FROM prices WHERE material_code NOT IN ({marks})", tuple(PRICES))   # 别的用例留下的价格先挪开
    db.execute(f"DELETE FROM prices WHERE material_code NOT IN ({marks})", tuple(PRICES))
    for code, p in PRICES.items():
        MAT.add_price(code, p, "actual", source="测试实价", price_date="2026-09-21")
    if R.CAL_PATH.exists():
        R.CAL_PATH.unlink()
    db.execute("DELETE FROM aba_trials")
    yield
    if R.CAL_PATH.exists():
        R.CAL_PATH.unlink()
    # 测试库是整轮共享的：把这里登记的实价和试验撤掉，别影响后面按估计价算的老用例
    db.execute("DELETE FROM prices WHERE source='测试实价'")
    db.execute("DELETE FROM aba_trials")
    for row in others:                                                           # 挪开的价格放回去
        if not db.q1("SELECT 1 FROM prices WHERE id=?", (row["id"],)):
            db.insert("prices", row)


def test_cost_layers_and_density(aba_prices):
    ctx = Ctx()
    ref = base_design(4.5)
    base_mix = (50 * 9670 + 25 * 8250) / 75
    assert ctx.cost(ref) == pytest.approx(base_mix, abs=1)                       # A、B 同基底
    d = base_design(4.5, filler=30)
    expect = 0.45 * base_mix + 0.55 * (0.7 * base_mix + 0.3 * 2500)
    assert ctx.cost(d) == pytest.approx(expect, abs=1)
    assert ctx.density(d) > ctx.density(ref) + 0.05                              # 填充把密度拉高
    assert d.whole()["FL"] == pytest.approx(16.5)
    assert "B 55%" in d.text() and "4.5 丝" in d.text()


def test_layer_rules(aba_prices):
    ctx = Ctx()
    bad = Design(a={"LL": 70, "FL": 30}, b={"LL": 100}, a_ratio=0.45, thickness=4.5)
    assert any("只能放 B 层" in x for x in ctx.check(bad))                       # 填充不能进表层
    over = Design(a={"LL": 100}, b={"LL": 30, "FL": 70}, a_ratio=0.45, thickness=4.5)
    assert any("超过上限" in x for x in ctx.check(over))
    assert ctx.check(base_design(4.5, filler=40, hdpe=10)) == []


def test_prediction_directions(aba_prices):
    ctx = Ctx()
    ref = base_design(4.5)
    f30 = ctx.predict(base_design(4.5, filler=30), ref)
    assert f30["td_rel"] < f30["md_rel"] < 1.0                                   # 填充伤横向更多
    assert f30["grade"] <= ctx.predict(ref, ref)["grade"]
    thick = ctx.predict(base_design(6.0), ref)
    assert thick["md_rel"] == pytest.approx(6.0 / 4.5, rel=1e-3)                 # 拉断力与厚度成正比
    assert thick["trans_score"] < ctx.predict(ref, ref)["trans_score"]          # 越厚越不透
    hd = ctx.predict(base_design(4.5, hdpe=10), ref)
    assert hd["md_rel"] > 1.0 > hd["td_rel"]                                     # HDPE 提纵向、伤横向


def test_optimize_per_grade(aba_prices):
    out = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": False})
    per = out["groups"]["现有料"]
    best = {g: items[0] for g, items in per.items() if items}
    assert set(best) == {0, 1, 2}
    assert best[2]["cost"] >= best[1]["cost"] >= best[0]["cost"]                 # 要求越透越贵
    for items in per.values():
        for ev in items:
            d: Design = ev["design"]
            assert ev["md_rel"] >= 1.0 - 1e-9 and ev["td_rel"] >= 1.0 - 1e-9
            assert 3.0 <= d.thickness <= 6.0
            assert "FL" not in d.a and ev["issues"] == []
            assert d.a.get("R1", 0) <= 33.4                                     # 表层再生不超过现配方水平
    assert best[0]["saving_pct"] > 5                                            # 透明度无所谓时能明显降本
    fixed = O.run({"md_req": 1.0, "td_req": 1.0, "t_min": 4.5, "t_max": 4.5, "use_new": False})
    assert all(ev["design"].thickness == 4.5 for items in fixed["groups"]["现有料"].values() for ev in items)
    text = O.to_text(out)
    assert "透光 ≥很好" in text and "降本" in text


def test_new_material_compared_with_current(aba_prices):
    MAT.upsert_material({"code": "mLL", "name": "茂金属 LLDPE", "category": "主体树脂", "grade": "SP1520", "density": 0.918})
    MAT.add_price("mLL", 9900, "actual", source="测试实价")                      # 比 LLDPE 贵：只有补强度时才可能划算
    out = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True})
    new = [ev for items in out["groups"]["加新料"].values() for ev in items]
    assert all(ev.get("vs_current", -2) < -1 for ev in new)                      # 列出来的都比现有料最优省
    MAT.add_price("mLL", 8800, "actual", source="测试实价")                      # 便宜又更强：一定该列出来
    out = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True})
    new = [ev for items in out["groups"]["加新料"].values() for ev in items]
    assert new and all(ev["vs_current"] < -1 and "mLL" in ev["design"].whole() for ev in new)
    assert all(ev["cost_m2"] == pytest.approx(ev["cost"] * ev["gsm"] / 1e6, rel=3e-3) for ev in new)


def test_price_scenarios_custom(aba_prices):
    s = P.build("custom", 10)
    assert s["prices"]["LL"] == pytest.approx(9670 * 1.10, abs=0.2)
    assert s["prices"]["R1"] == pytest.approx(8250 * 1.06, abs=0.2)             # 再生料按 0.6 跟随
    assert s["prices"]["FL"] == pytest.approx(2500)                              # 填充料不跟
    assert P.build("current")["ratio"] == 1.0


def test_price_scenario_futures(aba_prices, monkeypatch):
    kl = [{"d": "2026-09-19", "close": 7000.0, "settle": 7000.0}, {"d": "2026-09-21", "close": 7000.0, "settle": 7000.0},
          {"d": "2026-11-05", "close": 7350.0, "settle": 7350.0}]
    monkeypatch.setattr(P, "_kline", lambda sym, days: kl if sym == "L0" else [{"d": "2026-11-05", "close": 7500.0, "settle": 7500.0}])
    live = P.build("live")
    assert live["ratio"] == pytest.approx(7350 / 7000, rel=1e-4)                # 报价日 → 今天，期货涨了 5%
    fut = P.build("fut1")
    assert fut["ratio"] == pytest.approx(7500 / 7000, rel=1e-4)


def _simulate(trials):
    rows = []
    for t in trials:
        d = t["design"]
        w = d.whole()
        fl, r, hd = w.get("FL", 0) / 10, w.get("R1", 0) / 10, w.get("HD", 0) / 10
        md = 4.0 * d.thickness * (1 - 0.10 * fl - 0.02 * r + 0.05 * hd)
        td = 3.6 * d.thickness * (1 - 0.16 * fl - 0.02 * r - 0.03 * hd)
        s = 1.6 - 0.65 * fl - 0.10 * r - 0.25 * hd - 0.12 * (d.thickness - 4.5)
        g = 2 if s >= 1.5 else (1 if s >= 0.6 else 0)
        rows.append({"编号": t["code"], "实测厚度(丝)": d.thickness, "纵向拉断力(N)": round(md, 2),
                     "横向拉断力(N)": round(td, 2), "透光档(0一般/1好/2很好)": g})
    return pd.DataFrame(rows)


def test_experiment_loop(aba_prices):
    r1 = E.first_round(t_ref=4.5)
    assert len(r1["trials"]) == 14 and sum(t["purpose"] == "现配方对照" for t in r1["trials"]) == 3
    x = pd.read_excel(r1["xlsx"], sheet_name=None)
    assert {"试验配方", "数据记录", "说明"} <= set(x)
    assert r1["docx"].endswith(".docx") and Path(r1["docx"]).exists()
    assert E.calibrate()["ok"] is False                                          # 没回填不能校准
    res = E.import_results(_simulate(r1["trials"]))
    assert res["updated"] == 14 and not res["problems"]
    cal = E.calibrate()
    assert cal["ok"] and cal["md_N_ref"] == pytest.approx(4.0 * 4.5 * (1 - 0.02 * 3.33), rel=0.05)
    assert cal["grade_hit"] >= 85
    assert cal["roles"]["filler"]["td"] < R.ROLES["filler"]["td"]               # 数据说填充更伤横向，系数跟着变
    ctx = Ctx()
    assert ctx.predict(base_design(4.5), base_design(4.5)).get("md_N")           # 校准后有 N 的绝对值
    assert len(cal["trans_cov"]) == len(cal["trans_terms"])                      # 透光参数的不确定度也存了
    p = ctx.predict(base_design(4.5, filler=10, hdpe=10), base_design(4.5))
    assert p["trans_sd"] > 0 and p["grade"] <= p["grade_point"]                  # 承诺的档位不高于点估计
    novel = Design(a={"LL": 100}, b={"LL": 70, "POE": 30}, a_ratio=0.45, thickness=4.5)
    if ctx.role.get("POE") and "POE" in ctx.mats:
        assert ctx.trans_sd(novel) > ctx.trans_sd(base_design(4.5))              # 没试过的料更拿不准
    r2 = E.next_round({"md_req": 1.0, "td_req": 1.0, "use_new": False})
    purposes = [t["purpose"] for t in r2["trials"]]
    feasible = {g for g, items in O.run({"md_req": 1.0, "td_req": 1.0, "use_new": False})["groups"]["现有料"].items() if items}
    for g in feasible:                                                           # 每个可行的档位都有它最省的那组
        assert any(f"透光≥{R.GRADE_LABEL[g]} 预测最省" in p for p in purposes)
    assert "现配方对照" in purposes and any(p.startswith("探索") for p in purposes)
    non_ctrl = [t for t in E.trials() if t["purpose"] != "现配方对照"]           # 对照组每轮都重复做，用来看漂移
    assert len({t["design"].key() for t in non_ctrl}) == len(non_ctrl)          # 其余不重复做已做过的配方


def test_import_rejects_bad_grade(aba_prices):
    r1 = E.first_round(t_ref=4.5, n_points=6)
    df = pd.DataFrame([{"编号": r1["trials"][0]["code"], "纵向拉断力(N)": 15, "横向拉断力(N)": 14, "透光档(0一般/1好/2很好)": 5},
                       {"编号": "T9-99", "纵向拉断力(N)": 10}])
    res = E.import_results(df)
    assert any("不是 0/1/2" in p for p in res["problems"]) and any("不在试验记录" in p for p in res["problems"])


def test_chat_tools_and_roles(aba_prices):
    from polysage.agents import roles
    from polysage.agents import tools as T

    text, _ = T.run("aba_optimize", {"md_req": 1, "td_req": 1, "grade_min": -1, "use_new": False})
    assert "透光 ≥很好" in text and "已入配方库：H" in text and "采购方案" in text
    ev_text, _ = T.run("aba_evaluate", {"a_layer": {"LL": 66.7, "R1": 33.3}, "b_layer": {"LL": 53.4, "R1": 26.6, "FL": 20},
                                        "a_ratio": 0.45, "thickness": 5})
    assert "元/吨" in ev_text and "透光预测" in ev_text
    adv = roles.ROLES["advisor"]
    assert "aba_optimize" in adv.tools and "recommend_schemes" not in adv.tools
    sp = roles.system_prompt("advisor")
    assert "ABA" in sp and "撕裂" not in sp and "穿刺" not in sp


def test_task_upgrades_old_brief(home):
    from polysage import kb
    from polysage.pipeline import task

    kb.write_yaml(task.TASK_PATH, {"product": {"name": "PE 包装膜", "structure": "mono", "process": "吹膜"},
                                   "red_lines": ["拉伸强度", "撕裂强度", "穿刺力", "热封强度"],
                                   "search": {"max_hits_per_query": 9}})
    t = task.load()
    assert t["product"]["structure"] == "ABA" and t["search"]["max_hits_per_query"] == 9
    assert "纵向拉断力" in task.brief() and "撕裂" not in task.brief()


def test_plan_round_50_and_word_roundtrip(aba_prices):
    from docx import Document

    from polysage import wordout as W

    r = E.plan_round(50, t_ref=4.5)
    tr = r["trials"]
    assert len(tr) == 50 and len({t["design"].key() for t in tr}) == 50
    kinds = r["meta"]["counts"]
    assert kinds["现配方对照"] == 3 and kinds.get("降本", 0) >= 3 and kinds.get("建模", 0) >= 25
    ctx = Ctx()
    for t in tr:
        assert not ctx.check(t["design"]), t["design"].text()                      # 每组都满足层规则
        if t["purpose"].startswith(("降本", "提透光")):
            assert t["predicted"]["md_rel"] >= 0.99 and t["predicted"]["td_rel"] >= 0.99
        if t["purpose"].startswith("建模"):
            assert min(t["predicted"]["md_rel"], t["predicted"]["td_rel"]) >= E.MIN_REL - 1e-9
    ths = {t["design"].thickness for t in tr if t["purpose"].startswith("建模")}
    fls = {t["design"].b.get("FL", 0) for t in tr if t["purpose"].startswith("建模")}
    assert len(ths) >= 4 and len(fls) >= 4                                         # 因素真的铺开了
    # Word：方案 + 投料单 + 记录表；把记录表填上再传回来
    doc = Document(r["docx"])
    rec = next(t for t in doc.tables if t.rows[0].cells[0].text == "编号" and "纵向" in t.rows[0].cells[3].text)
    sim = _simulate(tr).set_index("编号")
    for row in rec.rows[1:]:
        code = row.cells[0].text
        row.cells[2].text = str(sim.loc[code, "实测厚度(丝)"])
        row.cells[3].text = str(sim.loc[code, "纵向拉断力(N)"])
        row.cells[4].text = str(sim.loc[code, "横向拉断力(N)"])
        row.cells[5].text = str(sim.loc[code, "透光档(0一般/1好/2很好)"])
    filled = Path(r["docx"]).with_name("filled.docx")
    doc.save(filled)
    res = E.import_results(str(filled))
    assert res["updated"] == 50 and not res["problems"]
    cal = E.calibrate()
    assert cal["ok"] and cal["grade_hit"] >= 80
    assert any("纵向" in h for t in W.read_tables(str(filled)) for h in t[0])


def test_markdown_to_word():
    from docx import Document
    import io

    from polysage import wordout as W

    md = """# 结论
现配方 **9200 元/吨**。

- 第一条
  - 子条
1. 编号一

| 档 | 元/吨 |
|---|---|
| 很好 | 9100 |
| 好 | 8800 |

```
code
```"""
    doc = Document(io.BytesIO(W.md_to_docx(md, "测试报告")))
    texts = [p.text for p in doc.paragraphs]
    assert "测试报告" in texts and "结论" in texts and any("9200 元/吨" in x for x in texts)
    assert doc.tables and doc.tables[0].rows[1].cells[1].text == "9100"


def test_campaign_clean_and_report(aba_prices, monkeypatch):
    from docx import Document

    from polysage.aba import campaign as C

    code = "MX9"
    MAT.upsert_material({"code": code, "name": "测试 LDPE 抽错价", "category": "主体树脂", "origin": "智能体发现"})
    MAT.add_price(code, 121000, "estimate", source="网查参考价（测试）", price_date="2026-09-24")
    MAT.add_price("FL", 2500, "actual", source="测试实价", price_date="2026-11-05")
    removed = C.clean_prices()
    assert any(code in x and "121000" in x for x in removed)
    assert not db.q("SELECT 1 FROM prices WHERE material_code=?", (code,))
    assert db.q("SELECT 1 FROM prices WHERE material_code='FL' AND price_type='actual'")      # 实价不动
    monkeypatch.setattr(C, "_scenarios", lambda: [P.build("current")])                      # 不联网取期货
    r = E.plan_round(24, t_ref=4.5)
    path = C.report_docx(r["round"], cleaned=removed)
    doc = Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "一、结论" in text and "每档最省" in text and "按吨和按面积" in text and "第 1 轮" in text
    assert "厚度不变（4.5 丝）时每档最省" in text and "goodsfu" not in text
    assert len(doc.tables) >= 4
    assert C.latest_files()["运行报告"].endswith(".docx")


def test_plan_round_new_materials_span_grades(aba_prices):
    MAT.upsert_material({"code": "RL", "name": "再生线性一级料（工业膜回料）", "category": "再生料", "is_recycled": 1,
                         "origin": "智能体发现 再生 PE 颗粒"})
    MAT.add_price("RL", 5150, "actual", source="测试实价", price_date="2026-11-05")
    r = E.plan_round(30, t_ref=4.5, n_new=4)
    new = [t for t in r["trials"] if t["purpose"].startswith("新料")]
    assert len(new) == 4 and all("RL" in t["design"].whole() for t in new)
    grades = {t["purpose"].split("，")[0] for t in new}
    assert len(grades) >= 2, grades                                                # 不全挤在一个透光档


def test_alternatives_need_fresh_sane_prices(aba_prices):
    import datetime as _dt

    old = (_dt.date.today() - _dt.timedelta(days=60)).isoformat()
    MAT.upsert_material({"code": "LLX", "name": "国产 C4 LLDPE 旧价", "category": "主体树脂"})
    MAT.add_price("LLX", 8000, "actual", source="旧日报（测试）", price_date=old)              # 旧价：不能拿来比
    MAT.upsert_material({"code": "LDX", "name": "某 LDPE 低得离谱", "category": "主体树脂", "grade": "2426H"})
    MAT.add_price("LDX", 5000, "actual", source="报价（测试）", price_date=_dt.date.today().isoformat())
    MAT.add_price("LD", 12500, "actual", source="测试实价", price_date=_dt.date.today().isoformat())
    out = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True})
    used = {k for items in out["groups"].get("加新料", {}).values() for ev in items for k in ev["design"].whole()}
    assert "LLX" not in used and "LDX" not in used
    note = " ".join(out["notes"])
    assert "LLX" in note and "天前" in note and "LDX" in note and "低太多" in note
    db.execute("DELETE FROM prices WHERE material_code IN ('LLX', 'LDX')")
    db.execute("DELETE FROM materials WHERE code IN ('LLX', 'LDX')")


def test_cheap_unbuyable_material_listed_as_lead(aba_prices):
    import datetime as _dt

    MAT.upsert_material({"code": "RLX", "name": "再生线性一级料（测试）", "category": "再生料", "is_recycled": 1})
    MAT.add_price("RLX", 5200, "estimate", source="网查参考价（测试）", price_date=_dt.date.today().isoformat())
    out = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True})
    note = " ".join(out["notes"])
    assert "询个价就能进方案" in note and "RLX" in note and "比 R1 便宜" in note
    db.execute("DELETE FROM prices WHERE material_code='RLX'")
    db.execute("DELETE FROM materials WHERE code='RLX'")



def test_fixed_thickness_groups_and_colored_materials(aba_prices):
    import datetime as _dt

    r = E.plan_round(30, t_ref=4.5, n_new=0)
    fixed = [t for t in r["trials"] if "厚度不变" in t["purpose"]]
    assert fixed and all(t["design"].thickness == 4.5 for t in fixed)              # 同厚度就能省的单独列出来
    thick = [t for t in r["trials"] if t["purpose"].startswith("降本") and t["design"].thickness > 4.5]
    assert thick and all("加厚到" in t["purpose"] and "按面积" in t["purpose"] for t in thick)
    # 黑色 / 有色回料：再便宜也不进透明膜的方案，也不当“询价线索”
    today = _dt.date.today().isoformat()
    MAT.upsert_material({"code": "RBK", "name": "再生高压黑色一级颗粒（测试）", "category": "再生料", "is_recycled": 1})
    MAT.add_price("RBK", 4000, "actual", source="测试实价", price_date=today)
    out = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True})
    used = {k for items in out["groups"].get("加新料", {}).values() for ev in items for k in ev["design"].whole()}
    assert "RBK" not in used and out["skipped"]["RBK"].startswith("有色")
    db.execute("DELETE FROM prices WHERE material_code='RBK'")
    db.execute("DELETE FROM materials WHERE code='RBK'")



def test_leads_need_plausible_recent_prices(aba_prices):
    import datetime as _dt

    today = _dt.date.today()
    MAT.add_price("LD", 12000, "actual", source="测试实价", price_date=today.isoformat())
    for code, name, price, d in (("LDQ", "某 LDPE 网查价低得离谱", 8000, today), ("RQO", "再生高压一级（旧价）", 5000, today - _dt.timedelta(days=500)),
                                 ("RQN", "再生高压一级（近价）", 6000, today - _dt.timedelta(days=20))):
        MAT.upsert_material({"code": code, "name": name, "category": "主体树脂" if code == "LDQ" else "再生料",
                             "is_recycled": 0 if code == "LDQ" else 1, "grade": "2426H" if code == "LDQ" else ""})
        MAT.add_price(code, price, "estimate", source="网查参考价（测试）", price_date=d.isoformat())
    out = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True})
    sk = out["skipped"]
    assert sk["RQN"].startswith("线索") and "低太多" in sk["LDQ"] and "一年多前" in sk["RQO"]
    db.execute("DELETE FROM prices WHERE material_code IN ('LDQ', 'RQO', 'RQN')")
    db.execute("DELETE FROM materials WHERE code IN ('LDQ', 'RQO', 'RQN')")
