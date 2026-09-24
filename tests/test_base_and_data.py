"""base 录入与过关线、双通道数据回灌、试验包、价格撤销、模型切换。"""
from __future__ import annotations

from conftest import seed_library

import pandas as pd
import pytest

from polysage import basedata, db, pricing
from polysage.config import Settings
from polysage.formulation import library as L
from polysage.formulation import materials as MAT
from polysage.ml import dataset as D

from _candidates import seed_candidates      # 测试候选材料池（正式库只预置 4 种基础料）


@pytest.fixture(autouse=True)
def _seed():
    MAT.seed_materials()
    seed_candidates()
    seed_library()


def test_base_entry_thresholds_and_judge():
    d = basedata.set_items({"tensile": {"mean": 32, "sd": 1.2, "n": 5}, "tear": {"mean": 3.5, "sd": 0.3, "n": 5},
                            "puncture": {"mean": 7.2, "sd": 0.5, "n": 5}, "seal": {"mean": 18.0, "sd": 1.5, "n": 5}},
                           thickness_um=50, seal_temp=130, note="测试")
    assert basedata.is_ready(d) and basedata.missing(d) == []
    thr = basedata.thresholds()
    assert thr["tensile"] == pytest.approx(max(32 * 0.95, 32 - 1.2))
    assert thr["seal"] == pytest.approx(max(18 * 0.95, 18 - 1.5))
    j = basedata.judge({"tensile": 31.0, "tear": 3.0, "puncture": 7.5})
    assert j["items"]["tensile"]["pass"] and not j["items"]["tear"]["pass"] and j["pass"] is False
    assert "膜厚 50" in basedata.brief() and "过关线" in basedata.to_markdown()
    # 数据层 base_stats 优先取 base.yaml
    assert D.base_stats(pd.DataFrame())["tensile"]["mean"] == 32
    # 无 SD 时只用 0.95
    d2 = basedata.set_items({"puncture": {"mean": 7.2}})
    assert basedata.thresholds()["puncture"] == pytest.approx(7.2 * 0.95)
    assert "base" in basedata.to_markdown()


def test_two_channel_upload_and_trial_kit(home):
    D.DATA_PATH = home / "chan" / "data.csv"
    D.DATA_PATH.parent.mkdir(exist_ok=True)
    if D.DATA_PATH.exists():
        D.DATA_PATH.unlink()
    p1, p2 = D.make_trial_kit("F04", batch_kg=20, n_samples=2)
    tpl = pd.read_csv(p1, encoding="utf-8-sig")
    assert list(tpl["sample_id"]) == ["F04-01", "F04-02"] and tpl["scheme_code"].iloc[0] == "F04" and float(tpl["mLL"].iloc[0]) == 10.0
    ws = pd.read_excel(p2, sheet_name="称料单")
    assert ws[ws["代码"] == "合计"]["用量 kg"].iloc[0] == 20
    tpl["tensile"] = [30.5, 31.0]
    tpl["tear"] = [3.6, 3.7]
    merged, issues = D.merge_upload(tpl, source="scheme", scheme_code="F04")
    own = tpl.copy()
    own["sample_id"] = ["OWN-01", "OWN-02"]
    own["scheme_code"] = ""
    merged, issues = D.merge_upload(own, source="own")
    df = D.load()
    assert set(df["source"]) == {"scheme", "own"} and (df["scheme_code"] == "F04").sum() == 2
    agg = D.aggregate(df)
    assert "source" in agg and len(agg) == 4


def test_price_undo_and_basis():
    before = MAT.current_prices()["LD"].get("actual")
    res = pricing.record_price("LD", 9100, "actual", source="测试")
    assert MAT.current_prices()["LD"]["actual"] == 9100
    undo = pricing.undo_last_price("LD", "actual")
    assert undo and undo["deleted"]["price"] == 9100
    assert MAT.current_prices()["LD"].get("actual") == before
    b = pricing.basis_summary()
    assert b["n_actual"] + b["n_estimate"] + b["n_missing"] == len(MAT.list_materials(active_only=True))


def test_llm_profile_switch(monkeypatch):
    monkeypatch.setenv("LLM_PROFILE", "zju")
    monkeypatch.setenv("ZJU_API_KEY", "k")
    monkeypatch.setenv("ZJU_CHAT_MODEL", "zju-qwen")
    monkeypatch.setenv("ZJU_SUPPORTS_THINKING", "0")
    s = Settings()
    assert s.chat_model == "zju-qwen" and s.chat_base_url.startswith("https://api.zjumembrane.cn") and s.chat_api_key == "k"
    assert s.chat_supports_thinking is False
    monkeypatch.setenv("LLM_PROFILE", "siliconflow")
    s2 = Settings()
    assert s2.chat_model == s2.sf_chat_model and s2.chat_supports_thinking is True
