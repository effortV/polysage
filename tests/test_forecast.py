"""价格预判：期货信号、模型+期货混合、入库、对方案成本的影响（不联网，假数据）。"""
from __future__ import annotations

from conftest import seed_library

from datetime import date

from polysage import db, forecast as F, llm
from polysage.formulation import materials as MAT


def test_next_contracts():
    assert F.next_contracts(date(2026, 9, 21), 2) == ["L2701", "L2705"]
    assert F.next_contracts(date(2026, 1, 2), 3) == ["L2605", "L2609", "L2701"]


def test_futures_signal_and_blend(monkeypatch, home):
    MAT.seed_materials()
    closes = [8000 + i * 10 for i in range(90)]  # 稳步上涨
    main = [{"d": f"2026-06-{(i % 28) + 1:02d}", "close": c, "settle": c} for i, c in enumerate(closes)]
    curve = {"L2701": [{"d": "x", "close": 8900, "settle": 8900}], "L2705": [{"d": "x", "close": 9100, "settle": 9100}]}
    monkeypatch.setattr(F, "fetch_kline", lambda symbol="L0", days=90: main if symbol == "L0" else curve.get(symbol, []))
    fs = F.futures_signal()
    assert fs["ok"] and fs["last"] == 8890 and fs["chg_20d"] > 0 and fs["term_pct"] > 0 and 0 < fs["implied_1m"] <= 10

    monkeypatch.setattr(F, "market_news", lambda family, limit=3: [{"title": "PE 周评", "url": "https://x/1", "date": date.today().isoformat(), "text": "供应偏紧，看涨" * 50}])
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"direction": "涨", "pct_1w": 1.0, "pct_1m": 4.0, "confidence": 0.6,
                                                                   "drivers": [{"text": "检修增多供应偏紧", "url": "https://x/1"}], "advice": "线性看涨，多用再生料"})
    rec = F.build("LLDPE", futures=fs)
    assert rec["direction"] == "涨" and rec["current"] and abs(rec["pct_1m"] - (0.6 * 4.0 + 0.4 * fs["implied_1m"])) < 0.01
    assert rec["price_1m"] == round(rec["current"] * (1 + rec["pct_1m"] / 100)) and rec["drivers"][0]["url"] == "https://x/1"
    lt = F.latest()
    assert "LLDPE" in lt and lt["LLDPE"]["pct_1m"] == rec["pct_1m"] and "线性 LLDPE" in F.outlook_text()
    scen = F.scenario_prices()
    assert scen["LL"] == scen["LLC"] == rec["price_1m"]
    from polysage.formulation import library as L

    seed_library()
    rows = F.scheme_impact(top_n=5)
    assert rows and all(r["cost_future"] >= r["cost_now"] for r in rows if "LL" in r["components"])   # 线性涨 → 含 LL 的方案成本升


def test_build_survives_model_failure(monkeypatch, home):
    MAT.seed_materials()
    monkeypatch.setattr(F, "market_news", lambda family, limit=3: [])
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    rec = F.build("HDPE", futures={"ok": False, "reason": "无网络"})
    assert rec["direction"] == "震荡" and rec["pct_1m"] == 0 and rec["confidence"] <= 0.1
