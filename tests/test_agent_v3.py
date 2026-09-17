"""V3：替代品窗口筛选、价格联动重排、推荐智能体、再生料批次。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from polysage import db, pricing, recommender
from polysage.formulation import constraints as C
from polysage.formulation import cost as COST
from polysage.formulation import generator as G
from polysage.formulation import library as L
from polysage.formulation import materials as MAT
from polysage.formulation import screening as S


@pytest.fixture(autouse=True)
def _seed():
    MAT.seed_materials()
    L.seed_top20()


def test_price_basis_and_auto_cost_limit():
    m = {x["code"]: x for x in MAT.list_materials()}
    assert m["LL"]["price"] == 12000 and m["R1"]["price"] == 8000 and m["LLC"]["price"] == 8400
    c = C.load()
    base_cost = COST.compute_simple(C.base_formulation(c))
    assert c["cost_limit"] == round(base_cost) and c.get("cost_limit_auto")
    assert c["cost_target"] == round(base_cost * 0.95)


def test_screening_windows_and_groups():
    res = S.screen("LL")
    assert res["windows"]["density"][0] < 0.918 < res["windows"]["density"][1]
    flat = {r.code: r for r in res["flat"]}
    assert flat["LLC"].verdict == "直接替代候选" and flat["LLC"].cheaper and flat["LLC"].fit_score == 1.0
    assert flat["POE"].fits["密度"] == "超窗口"
    assert "再生料（成本填充）" in res["groups"] and flat["R1"].cheaper
    rows = S.to_rows(res)
    assert rows and {"分组", "代码", "结论"} <= set(rows[0])


def test_generator_allowed_and_llc_theme():
    cands = G.generate(n_out=10, n_samples=3000, seed=5, allowed=["LL", "LLC", "R1", "LD", "HD"])
    assert cands
    for cd in cands:
        assert set(cd.components) <= {"LL", "LLC", "R1", "LD", "HD", "AD"}
        assert not any(k == "LL" and 0 < v < 8 for k, v in cd.components.items())


def test_recommender_and_price_linkage(fake_llm):
    inp = recommender.example_input()
    inp["use_llm"] = False
    out = recommender.recommend(inp)
    assert out["schemes"] and out["schemes"][0]["rank"] == 1
    assert all(s["cost"] < out["cost_limit"] for s in out["schemes"])
    assert out["screening"] and Path(out["outputs"][0]).exists()
    assert db.q1("SELECT COUNT(*) c FROM recommend_runs")["c"] >= 1
    top_before = out["schemes"][0]["formula"]
    # 价格联动：LLC 大幅涨价 → 含 LLC 的方案成本上升并被标记
    r = pricing.record_price("LLC", 11500, "actual", source="测试报价")
    assert r["n_flagged"] >= 1 and pricing.REPORT_PATH.exists()
    lib = {f["code"]: f for f in L.list_formulations()}
    a01 = next(f for f in lib.values() if f["formula"] == top_before)
    assert a01["cost_tier"] in ("混合", "实价") and a01["cost_used"] > out["schemes"][0]["cost"]
    # 有 LLM（假）时走定性评估
    out2 = recommender.recommend({"use_llm": True, "n_schemes": 5, "save_to_library": False})
    assert out2["mode"].startswith("知识驱动") and out2["schemes"][0].get("effects_short")
    db.execute("DELETE FROM prices WHERE material_code='LLC' AND price_type='actual'")
    L.recompute_costs()


def test_recycled_batches():
    rid = MAT.add_recycled_batch({"material_code": "R1", "supplier": "供应商A", "grade": "一级透明", "batch_no": "B1", "mfr": 2.1, "density": 0.921,
                                  "ash": 0.2, "gel_count": 30, "price": 7900, "received_at": "2026-09-16"})
    assert rid and MAT.list_recycled_batches("R1")[0]["batch_no"] == "B1"
    assert (MAT.KB["material"] / "再生料批次记录.csv").exists()
