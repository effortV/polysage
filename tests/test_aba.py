"""ABA 三层膜：成本、定性预测、分档寻优、价格场景、实验闭环、对话工具。"""
from __future__ import annotations

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
    x = pd.read_excel(r1["path"], sheet_name=None)
    assert {"试验配方", "数据记录", "说明"} <= set(x)
    assert E.calibrate()["ok"] is False                                          # 没回填不能校准
    res = E.import_results(_simulate(r1["trials"]))
    assert res["updated"] == 14 and not res["problems"]
    cal = E.calibrate()
    assert cal["ok"] and cal["md_N_ref"] == pytest.approx(4.0 * 4.5 * (1 - 0.02 * 3.33), rel=0.05)
    assert cal["grade_hit"] >= 85
    assert cal["roles"]["filler"]["td"] < R.ROLES["filler"]["td"]               # 数据说填充更伤横向，系数跟着变
    ctx = Ctx()
    assert ctx.predict(base_design(4.5), base_design(4.5)).get("md_N")           # 校准后有 N 的绝对值
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
