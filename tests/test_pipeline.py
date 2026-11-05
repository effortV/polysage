"""流水线离线端到端：①～⑥ 发现阶段 + ⑦～⑨ 实验闭环（假 LLM + 假检索源）。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from polysage import db, kb
from polysage.config import KB
from polysage.pipeline import runner, state, task



def test_discovery_pipeline(fake_llm, fake_sources):
    db.execute("DELETE FROM aba_trials")          # 别的用例留下的试验会让 ⑥ 只导出旧轮次
    runner.run_discovery(resume=False, echo=None, max_hits_per_query=3, max_keep=40, do_prices=True)
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
    assert len(st["design"]["top"]) >= 3 and all("丝" in r["formula"] for r in st["design"]["top"])   # ABA 分档方案
    assert all(Path(p).exists() for p in st["design"]["outputs"])
    docs = [Path(p) for p in st["report"]["outputs"]]
    assert all(p.exists() and p.stat().st_size > 5000 for p in docs)
    from polysage.aba import experiments as AE

    assert len(AE.trials()) >= 14                                   # ⑥ 首轮：3 个厚度对照 + D-最优
    # 新材料是智能体从网页上找来的：每条都带来源网址，而不是凭空补的清单
    found = db.q("SELECT code, name, source_url FROM materials WHERE COALESCE(source_url,'') <> ''")
    assert found and all(r["source_url"].startswith("http") for r in found)


def _simulated_results(trials):
    rows = []
    for t in trials:
        d = t["design"]
        w = d.whole()
        fl, r, hd = w.get("FL", 0) / 10, w.get("R1", 0) / 10, w.get("HD", 0) / 10
        s = 1.6 - 0.65 * fl - 0.10 * r - 0.25 * hd - 0.12 * (d.thickness - 4.5)
        rows.append({"编号": t["code"], "实测厚度(丝)": d.thickness,
                     "纵向拉断力(N)": round(4.0 * d.thickness * (1 - 0.10 * fl - 0.02 * r + 0.05 * hd), 2),
                     "横向拉断力(N)": round(3.6 * d.thickness * (1 - 0.16 * fl - 0.02 * r - 0.03 * hd), 2),
                     "透光档(0一般/1好/2很好)": 2 if s >= 1.5 else (1 if s >= 0.6 else 0)})
    return pd.DataFrame(rows)


def test_experiment_loop(fake_llm, fake_sources):
    """⑥ 首轮 → 回填 → ⑦ 校准 → ⑧ 下一轮 → ⑨ 复盘，全走 ABA。"""
    from polysage.aba import experiments as AE
    from polysage.formulation import materials as MAT

    MAT.seed_materials()
    for code, price in {"LL": 9670, "R1": 8250, "FL": 2500, "HD": 9300}.items():
        MAT.add_price(code, price, "actual", source="测试实价")
    db.execute("DELETE FROM aba_trials")
    AE.reset_calibration()
    try:
        doe = runner.run_stage("doe", echo=None)
        assert doe["n_design"] == 14
        assert AE.import_results(_simulated_results(AE.trials()))["updated"] == 14
        learn = runner.run_stage("learn", echo=None)
        assert learn["n"] == 14 and learn["md_N_ref"] and learn["grade_hit"] >= 80
        scan = runner.run_stage("scan", echo=None)
        assert scan["n"] >= 5 and Path(scan["outputs"][0]).exists()
        review = runner.run_stage("review", echo=None, round_no=1)
        assert Path(review["outputs"][0]).exists()
    finally:
        AE.reset_calibration()
        db.execute("DELETE FROM prices WHERE source='测试实价'")
        db.execute("DELETE FROM aba_trials")


def test_task_and_state_roundtrip():
    t = task.load()
    t["product"]["thickness"] = "固定 5 丝"
    task.save(t)
    assert task.load()["product"]["thickness"] == "固定 5 丝"
    assert "固定 5 丝" in task.brief()
    state.set_stage("collect", status="done", n_hits=1)
    assert state.stage("collect")["n_hits"] == 1
