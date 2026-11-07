"""一轮多少组可调（最多 150）、没回填的一轮可以替换；回填后出下一轮：先自动校准、按当天的价格挑、
建模组避开做过的；报告写上一轮学到了什么、价格变了多少。"""
from __future__ import annotations

import json

from polysage.aba import campaign as C
from polysage.aba import experiments as E
from polysage.aba import prices as P
from polysage.aba import rules as R
from polysage.config import KB
from polysage.formulation import materials as MAT

from test_aba import _simulate, aba_prices  # noqa: F401  （共用夹具：钉死四种料的价格、清空试验）


def test_round_size_and_replace_untested_round(aba_prices):  # noqa: F811
    r = E.plan_round(80, t_ref=4.5)
    assert r["round"] == 1 and len(r["trials"]) == 80 and len(E.trials(1)) == 80
    assert r["meta"]["counts"]["现配方对照"] == 3 and r["meta"]["counts"]["建模"] > 40
    assert r["meta"]["calibration"]["ok"] is False                                 # 还没回填：按定性系数
    r2 = E.plan_round(40, t_ref=4.5, replace=True)                                 # 第 1 轮还没做：换成 40 组
    assert r2["round"] == 1 and r2["meta"]["replaced"] == 80 and len(E.trials(1)) == 40 and not E.trials(2)
    assert len(E.plan_round(500, t_ref=4.5, replace=True)["trials"]) == E.MAX_ROUND   # 上限
    E.plan_round(30, t_ref=4.5, replace=True)
    E.import_results(_simulate(E.trials(1)[:5]))                                   # 回填了几组就不能再替换
    r3 = E.plan_round(30, t_ref=4.5, replace=True)
    assert r3["round"] == 2 and r3["meta"]["replaced"] == 0 and len(E.trials(1)) == 30
    assert sum(t["purpose"] == "现配方对照" for t in r3["trials"]) == 3              # 对照每轮都重做


def test_next_round_learns_from_results_and_prices(aba_prices, monkeypatch):  # noqa: F811
    r1 = E.plan_round(24, t_ref=4.5)
    assert r1["meta"]["prices"]["LL"]["price"] == 9670
    E.import_results(_simulate(r1["trials"]))
    assert not R.calibration()                                                    # 回填了但还没校准
    MAT.add_price("LL", 10200, "actual", source="测试实价", price_date="2026-09-22")  # 价格变了
    r2 = E.plan_round(30, t_ref=4.5)
    cal = r2["meta"]["calibration"]
    assert cal["ok"] and cal["fresh"] and cal["n"] == 24 and R.calibration()["n"] == 24   # 出方案前自动校准
    assert r2["meta"]["prices"]["LL"]["price"] == 10200 and r2["meta"]["prior_measured"] == 24
    assert any(t["purpose"].startswith("建模：补以前没测过的区域") for t in r2["trials"])
    done = {t["design"].key() for t in E.trials(1) if t["purpose"] != "现配方对照"}
    assert not done & {t["design"].key() for t in r2["trials"] if t["purpose"] != "现配方对照"}   # 不重复做过的
    assert E.ensure_calibrated()["fresh"] is False                                 # 数据没变不重算
    assert E.round_prices(1)["LL"] == 9670 and E.round_prices(2)["LL"] == 10200

    monkeypatch.setattr(C, "_scenarios", lambda: [P.build("current")])            # 不联网取期货
    rep = C.report_blocks(2, t_ref=4.5)
    heads = [b["text"] for b in rep["blocks"] if b["t"] == "h" and b.get("level") == 1]
    assert heads[0].startswith("一、结论") and heads[1] == "二、上一轮反馈：价格变化与实测" and heads[2] == "三、价格"
    concl = " ".join(rep["blocks"][1]["items"])
    assert "价格（和第 1 轮比）" in concl and "LL 9670→10200" in concl and "实验反馈" in concl and "已用 24 组实测校准" in concl
    texts = json.dumps(rep["blocks"], ensure_ascii=False)
    assert "模型学到了什么" in texts and "预测 vs 实测" in texts and "先验→校准" in texts
    best = E.verified_best()
    for g, b in best.items():                                                      # 实测验证过的最省：真的达标
        assert b["grade"] >= g and b["md_n"] >= b["md_ref"] - 1e-6 and b["td_n"] >= b["td_ref"] - 1e-6


def test_round_prices_from_old_report():
    """老的轮次 meta 里没记价格：从运行报告的价格表里取。"""
    rn = 97
    p = KB["experiment"] / f"round_{rn}_report.json"
    p.write_text(json.dumps({"blocks": [{"t": "table", "rows": [{"代码": "LL", "名称": "LLDPE", "当前价(元/吨)": 9780.0},
                                                                {"代码": "FL", "名称": "填充", "当前价(元/吨)": 2500}]}]},
                            ensure_ascii=False), encoding="utf-8")
    try:
        assert E.round_prices(rn) == {"LL": 9780.0, "FL": 2500.0}
    finally:
        p.unlink()
