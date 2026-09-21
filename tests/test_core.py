"""核心层单元测试：成本/约束/生成器、入库与检索、工具、ML。"""
from __future__ import annotations

from conftest import seed_library

import numpy as np
import pandas as pd
import pytest

from polysage import db
from polysage.formulation import constraints as C
from polysage.formulation import cost as COST
from polysage.formulation import generator as G
from polysage.formulation import library as L
from polysage.formulation import materials as MAT


@pytest.fixture(autouse=True)
def _seed():
    MAT.seed_materials()
    seed_library()


def _doc_mats():
    """内部技术路线附录 D 的假设价（用于核对成本公式与文档一致）。"""
    mats = {m["code"]: dict(m, price_actual=None) for m in MAT.list_materials(active_only=False)}
    for code, p in {"LL": 8400, "LD": 9600, "HD": 8400, "R1": 6400, "AD": 12500}.items():
        mats[code]["price_estimate"] = p
    return mats


def test_cost_matches_internal_doc():
    mats = _doc_mats()
    assert round(COST.compute_simple(C.base_formulation(), mats)) == 8020
    r = COST.compute({"LL": 45, "LD": 10, "HD": 5, "R1": 39, "AD": 1}, C.base_formulation(), mats)
    assert round(r.cost_estimate) == 7781 and r.cost_actual is None
    assert 0.92 < r.density < 0.93 and 96 < r.area_index < 98
    # 当前价格卡口径：LLDPE 12,000 / 再生 8,000（甲方口述）
    assert round(COST.compute_simple(C.base_formulation())) == 10580


def test_constraints_detect_violations():
    assert C.check({"LL": 45, "LD": 10, "HD": 5, "R1": 39, "AD": 1}) == []
    issues = C.check({"LL": 10, "R1": 89, "AD": 1})
    assert any("新料 PE" in i for i in issues) and any("再生料合计" in i for i in issues)
    assert any("填充母料" in i for i in C.check({"LL": 40, "LD": 10, "R1": 34, "FL": 15, "AD": 1}))


def test_generator_feasible_and_cheaper():
    limit = C.load()["cost_limit"]
    cands = G.generate(n_out=15, n_samples=4000, seed=3)
    assert len(cands) >= 10
    for cd in cands:
        assert abs(sum(cd.components.values()) - 100) < 0.11
        assert C.check(cd.components) == []
        assert cd.cost is not None and cd.cost < limit
        assert all(v >= 2 or k in ("AD", "CP") for k, v in cd.components.items())


def test_actual_price_overrides_estimate():
    db.execute("DELETE FROM prices WHERE material_code='LD' AND price_type='actual'")
    MAT.add_price("LD", 9000, "actual", source="甲方回填")
    r = COST.compute({"LD": 100})
    assert r.cost_actual == 9000 and r.cost_used == 9000 and r.cost_estimate == 9600 and r.tier == "实价"
    assert MAT.get_material("LD")["price_tier"] == "实价"
    mixed = COST.compute({"LD": 50, "HD": 50})
    assert mixed.tier == "混合" and mixed.cost_actual is None and mixed.cost_used == 0.5 * 9000 + 0.5 * mixed.breakdown[1]["price_estimate"]
    db.execute("DELETE FROM prices WHERE material_code='LD' AND price_type='actual'")
    MAT.export_price_card()


def test_ingest_and_search(fake_llm, fake_sources):
    from polysage.ingest import ingest_hit, ingest_text
    from polysage.rag.index import search
    from polysage.sources.base import SearchHit

    sid, note = ingest_hit(SearchHit(provider="openalex", external_id="W1", title="mLLDPE dart impact", source_type="paper",
                                     doi="10.1/x", abstract="Metallocene LLDPE improves dart impact of blown film."), fetch_fulltext=False)
    assert sid and db.q1("SELECT n_chunks FROM sources WHERE id=?", (sid,))["n_chunks"] >= 1
    sid2, note2 = ingest_hit(SearchHit(provider="crossref", external_id="x", title="mLLDPE dart impact", source_type="paper", doi="10.1/x"), fetch_fulltext=False)
    assert sid2 == sid and "已存在" in note2
    ingest_text("PPA 含氟加工助剂可消除 LLDPE 鲨鱼皮熔体破裂。", title="PPA 说明", source_type="tds", credibility=2)
    hits = search("鲨鱼皮 加工助剂", top_k=3, use_rerank=False)
    assert hits and "鲨鱼皮" in hits[0]["text"]


def test_tools_and_session(fake_llm):
    from polysage.agents import runner, tools as T

    comps = {"LL": 45, "LD": 10, "HD": 5, "R1": 39, "AD": 1}
    txt, _ = T.run("compute_cost", {"components": comps})
    assert str(round(COST.compute_simple(comps))) in txt
    txt, _ = T.run("check_constraints", {"components": {"LL": 10, "R1": 89, "AD": 1}})
    assert "违反" in txt
    sid = runner.create_session("formulation")
    res = runner.chat(sid, "给我算一下 F01 的成本")
    assert res["content"]
    assert runner.messages(sid)[0]["role"] == "user"
    nid = runner.archive_session(sid)
    assert nid and runner.get_session(sid)["archived"] == 1


def test_ml_roundtrip(home):
    from polysage.ml import dataset as D, doe, models as M, optimize as O

    (home / "mltest").mkdir(exist_ok=True)
    D.DATA_PATH = home / "mltest" / "data.csv"
    rng = np.random.default_rng(0)
    from polysage.pipeline.stage_experiment import VARIABLES

    design = doe.first_round(n_points=20, variables=VARIABLES, priority=[f["comps"] for f in L.SEED_TOP20[:4]], seed=1)
    assert doe.coverage_report(design, VARIABLES)["constraint_violations"] == 0

    def truth(r):  # 让茂金属能补偿 LLDPE 减量，保证存在过关解
        ll, mll, ld, hd, r1, rl, poe = [r.get(k, 0) for k in ["LL", "mLL", "LD", "HD", "R1", "RL", "POE"]]
        return (22 + 0.03 * ll + 0.12 * mll + 0.05 * hd - 0.01 * r1, 400 + 2 * ll + 8 * mll + 2 * ld - 20 * hd - 1.0 * r1 + 1.5 * rl + 15 * poe,
                5 + 0.02 * ll + 0.25 * mll - 0.05 * hd - 0.01 * r1 + 0.2 * poe, 15 + 0.02 * ll + 0.15 * mll + 0.06 * ld - 0.1 * hd - 0.01 * r1,
                8 - 0.01 * ll - 0.04 * mll - 0.03 * ld + 0.4 * hd + 0.03 * r1 + 0.02 * rl)

    rows = []

    def add(sid, comps, reps=2):
        t = truth(comps)
        for _ in range(reps):
            rows.append({"sample_id": sid, **{k: comps.get(k, 0) for k in D.COMP_COLS}, "R1_batch": "B1", "thickness_um": 50,
                         "tensile": t[0] + rng.normal(0, 0.3), "tear": t[1] + rng.normal(0, 8), "puncture": t[2] + rng.normal(0, 0.15),
                         "seal": t[3] + rng.normal(0, 0.3), "haze": t[4] + rng.normal(0, 0.2)})

    add("S0-01", {"LL": 60, "LD": 10, "HD": 5, "R1": 25}, 3)
    for _, r in design.iterrows():
        add(r["sample_id"], {k: float(r[k]) for k in D.COMP_COLS if k in r})
    df = pd.DataFrame(rows)
    D.save(df)
    assert D.qc(df) == []
    reports, summary = M.train_all(df)
    assert {r.target for r in reports} == set(D.PRIMARY_TARGETS)
    assert all(r.metrics for r in reports)
    assert summary["pass_accuracy"] >= 0.8
    pred = M.predict([{"LL": 30, "mLL": 12, "LD": 8, "HD": 5, "R1": 44, "AD": 1}])
    assert "tensile_mean" in pred and np.isfinite(pred["tensile_mean"].iloc[0])
    rec = O.recommend(seed=1)
    assert len(rec["recommendations"]) >= 1
    assert (rec["recommendations"]["cost"] < C.load()["cost_limit"]).all()
    pf = O.pareto_front(rec["candidates"].head(200))
    assert len(pf) >= 1
