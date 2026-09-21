"""流水线离线端到端：①～⑥ 发现阶段 + ⑦～⑨ 实验闭环（假 LLM + 假检索源）。"""
from __future__ import annotations

from conftest import seed_library

from pathlib import Path

import numpy as np
import pandas as pd

from polysage import db, kb
from polysage.config import KB
from polysage.pipeline import runner, stage_experiment, state, task


def test_discovery_pipeline(fake_llm, fake_sources):
    res = runner.run_discovery(resume=False, echo=None, max_hits_per_query=3, max_keep=40, do_prices=True)
    st = state.load()["stages"]
    for key in ("collect", "mechanism", "scout", "design", "report", "doe"):
        assert st[key]["status"] == "done", (key, st[key].get("error"))
    assert st["collect"]["n_kept"] > 0 and st["collect"]["n_ingested"] > 0
    assert (KB["project"] / "来源清单.xlsx").exists()
    report = kb.read_text(KB["project"] / "现配方机理报告.md")
    assert "## 4" in report and "[S" in report and "## 出处" in report
    assert st["scout"]["n_cards"] > 0 and st["scout"]["n_prices"] > 0
    assert db.q1("SELECT COUNT(*) c FROM prices WHERE price_type='estimate' AND url LIKE 'https://example.com/%'")["c"] > 0
    assert (KB["project"] / "候选材料全清单.xlsx").exists()
    assert len(st["design"]["top"]) == 20
    assert (KB["formulation"] / "Top20候选配方_初步预计.xlsx").exists() and (KB["price"] / "询价清单.xlsx").exists()
    docs = [Path(p) for p in st["report"]["outputs"]]
    assert all(p.exists() and p.stat().st_size > 5000 for p in docs)
    assert stage_experiment.count_design() >= 24
    assert (KB["experiment"] / "数据表模板.csv").exists()
    # 新材料建议被写入原料库
    assert db.q1("SELECT id FROM materials WHERE code='LL8'")


def test_experiment_loop(fake_llm, fake_sources):
    from polysage.formulation import library as L, materials as MAT
    from polysage.ml import dataset as D

    MAT.seed_materials()
    seed_library()
    design = stage_experiment.doe_round1(priority=[f["comps"] for f in L.SEED_TOP20[:6]])
    df_design = pd.read_excel(design, sheet_name="试验配方")
    rng = np.random.default_rng(1)

    def truth(r):
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
    for _, r in df_design.iterrows():
        add(r["sample_id"], {k: float(r[k]) for k in D.COMP_COLS if k in r})
    D.save(pd.DataFrame(rows))
    out = runner.run_stage("learn", echo=None)
    assert out["models"] and (KB["model"] / "模型验证报告.md").exists()
    rr = stage_experiment.record_round(1)
    assert rr["round"] == 1
    out = runner.run_stage("scan", echo=None)
    assert out["n_recommended"] >= 1 and (KB["experiment"] / "推荐配方.xlsx").exists()
    assert db.q1("SELECT COUNT(*) c FROM formulations WHERE status='推荐'")["c"] >= 1
    out = runner.run_stage("review", echo=None, round_no=1)
    assert out["n_samples"] > 0 and Path(out["outputs"][0]).exists()
    assert "轮次" in runner.status_text()


def test_task_and_state_roundtrip():
    t = task.load()
    t["product"]["thickness_um"] = 50
    task.save(t)
    assert task.load()["product"]["thickness_um"] == 50
    assert "50 μm" in task.brief()
    state.set_stage("collect", status="done", n_hits=1)
    assert state.stage("collect")["n_hits"] == 1
