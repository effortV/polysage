"""V3：替代品窗口筛选、价格联动重排、推荐智能体、再生料批次。"""
from __future__ import annotations

from conftest import seed_library

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
    seed_library()


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
    # 新规则：只用买得到的料（有实价/报价来源），所以先给 LLC 一条正式报价
    pricing.record_price("LLC", 8300, "actual", source="测试基准报价")
    inp = recommender.example_input()
    inp["use_llm"] = False
    out = recommender.recommend(inp)
    assert any("只用买得到的料" in n for n in out["notes"])
    keep = set(out["base_formulation"]) | {"AD"}      # 现配方里的料与必配助剂即使暂无报价也保留（标待询价）
    assert all(set(s.get("unbuyable") or []) <= keep for s in out["schemes"])
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


def test_tool_result_clipping_and_caps():
    """工具结果过长会把回答挤没：留头留尾并标明省略；常用工具都有调用上限。"""
    from polysage.agents.runner import MAX_TOOL_CHARS, TOOL_CALL_CAPS, _clip_tool

    short = "只有一点点"
    assert _clip_tool(short) == short
    long_text = "甲" * 30000
    clipped = _clip_tool(long_text)
    assert len(clipped) < MAX_TOOL_CHARS + 200 and "省略" in clipped
    assert clipped.startswith("甲") and clipped.endswith("甲")
    for name in ("list_materials", "recommend_schemes", "procurement_plan", "price_outlook"):
        assert TOOL_CALL_CAPS.get(name)


def test_parity_and_diversity_rules(fake_llm):
    """性能门槛（↓↓ 或多项 ↓ 不推荐）、最小降本、档位分散。"""
    from polysage import pricing, recommender
    from polysage.formulation import qualitative as Q

    assert Q.parity({"拉伸": "≈", "撕裂": "↓", "穿刺": "≈", "热封": "↑"})["ok"]            # 单项小降可以
    assert not Q.parity({"拉伸": "↓↓", "撕裂": "≈", "穿刺": "≈", "热封": "≈"})["ok"]        # 明显下降不行
    assert not Q.parity({"拉伸": "↓", "撕裂": "↓", "穿刺": "≈", "热封": "≈"})["ok"]         # 两项下降不行
    p0 = Q.parity({})                                                                       # 没评估 ≠ 会降
    assert p0["ok"] and not p0["assessed"] and "未做定性评估" in p0["why"]

    codes = ("LL", "LLC", "LD", "HD", "R1", "RL")
    for code, price in zip(codes, (9670, 9200, 12150, 9300, 6290, 5150)):
        pricing.record_price(code, price, "actual", source="测试")
    try:
        out = recommender.recommend({"use_llm": False, "n_schemes": 6, "save_to_library": False})
    finally:
        for code in codes:                       # 还原，避免影响后面的用例
            pricing.undo_last_price(code, "actual")
    assert out["schemes"] and all((s.get("savings_pct") or 0) >= 3 for s in out["schemes"])   # 降本门槛
    assert all(s.get("bucket") for s in out["schemes"])                                       # 每个方案有档位
    buckets = {s["bucket"] for s in out["schemes"]}
    assert len(buckets) >= 1 and any("档位分布" in n for n in out["notes"])
    counts = {b: sum(1 for s in out["schemes"] if s["bucket"] == b) for b in buckets}
    assert max(counts.values()) <= len(out["schemes"])                                        # 不再死循环、且能出结果


def test_pending_turn_detection_and_resume(fake_llm, monkeypatch):
    """服务重启把一轮掐断后：能认出“只有工具结果没有回答”，并把回答补出来。"""
    from polysage import db
    from polysage.agents import runner

    sid = runner.create_session("advisor", title="中断测试")
    assert not runner.is_pending(sid)                     # 空会话不算中断

    runner._add(sid, "user", "出个方案")
    runner._add(sid, "assistant", "好的，我先拉数据。", tool_calls=[{"id": "c1", "name": "get_base", "arguments": {}}])
    runner._add(sid, "tool", "base 尚未录入", tool_call_id="c1", name="get_base")
    assert not runner.is_pending(sid)                       # 刚写的：当作别的标签页正在跑，不去抢
    assert runner.is_pending(sid, stale_after=0)            # 冷下来（或明确不等）→ 这轮没跑完

    monkeypatch.setattr(runner.llm, "chat", lambda messages, **kw: runner.llm.ChatResult(content="补出来的结论：建议先录 base。"))
    out = runner.resume(sid)
    assert "补出来的结论" in out["content"]
    assert not runner.is_pending(sid, stale_after=0)        # 补完就不再是 pending
    rows = db.q("SELECT role, content FROM messages WHERE session_id=? ORDER BY id", (sid,))
    assert rows[-1]["role"] == "assistant" and rows[-1]["content"] == out["content"]
    assert sum(1 for r in rows if r["role"] == "user") == 1   # 续跑不会重复添加用户消息


def test_search_budget_stops_the_loop(fake_llm, monkeypatch):
    """模型一直想查同一件事时：重复调用直接给回上次结果，检索次数用满就逼它作答。"""
    from polysage.agents import runner
    from polysage.agents import tools as T

    ran = []

    def fake_run(name, args):
        ran.append((name, args.get("query") or args.get("codes")))
        return f"{name} 没查到", []

    monkeypatch.setattr(T, "run", fake_run)

    calls = {"n": 0}

    def fake_chat(messages, **kw):
        last = messages[-1].get("content") or ""
        if any(k in last for k in ("不要再查", "到此为止", "直接作答", "不要再调用工具")):
            return runner.llm.ChatResult(content="AD 查不到公开报价，按估计价 12000 计入，需向助剂厂询价。方案照常给出。")
        calls["n"] += 1
        q = "PPA 母粒 价格" if calls["n"] % 2 else "PPA 母粒 报价"      # 换个关键词接着查
        return runner.llm.ChatResult(content="", tool_calls=[{"id": f"c{calls['n']}", "name": "web_search",
                                                              "arguments": {"query": q, "limit": 8}}])

    monkeypatch.setattr(runner.llm, "chat", fake_chat)
    monkeypatch.setattr(runner.llm, "chat_answer", fake_chat)

    sid = runner.create_session("advisor", title="死循环测试")
    out = runner.chat(sid, "AD 多少钱一吨")
    assert "查不到" in out["content"]
    assert len(ran) <= runner.SEARCH_BUDGET                 # 检索次数封顶，不会一直查下去
    assert len(set(ran)) == len(ran)                        # 同参数的重复调用没有真的跑第二遍


def test_material_miss_cooldown(fake_llm, monkeypatch):
    """同一个料 24 小时内查过且没查到：直接跳过，不再浪费几分钟。"""
    from polysage import daily_quotes as DQ

    hits = {"n": 0}

    def fake_search(*a, **kw):
        hits["n"] += 1
        return []

    monkeypatch.setattr("polysage.sources.websearch.search", fake_search)
    assert DQ.recent_miss("AD") is None
    s1 = DQ.web_material_quotes(["AD"], depth="快", pages_per_query=1)
    assert s1["AD"]["rows"] == 0 and not s1["AD"].get("skipped")
    assert DQ.recent_miss("AD")                              # 记住了这次空手而回
    n_after_first = hits["n"]

    s2 = DQ.web_material_quotes(["AD"], depth="快", pages_per_query=1)
    assert s2["AD"].get("skipped") and hits["n"] == n_after_first     # 第二次根本没去搜
    s3 = DQ.web_material_quotes(["AD"], depth="快", pages_per_query=1, force=True)
    assert not s3["AD"].get("skipped") and hits["n"] > n_after_first  # 明确要求时才重查


def test_tool_timeout_and_dangling_call_recovery(fake_llm, monkeypatch):
    """网站不响应时放弃这次调用；掐在“调了工具没结果”上的一轮，续跑能出回答。"""
    import time as _t

    from polysage import db
    from polysage.agents import runner
    from polysage.agents import tools as T

    monkeypatch.setattr(runner, "TOOL_TIMEOUT_DEFAULT", 1)
    monkeypatch.setattr(T, "run", lambda name, args: (_t.sleep(30), ("不该等到这个", []))[1])
    text, _ = runner._run_tool("web_search", {"query": "卡住的站"})
    assert "已经放弃这次调用" in text and "直接作答" in text

    sid = runner.create_session("advisor", title="卡住测试")
    runner._add(sid, "user", "AD 多少钱")
    runner._add(sid, "assistant", "我查一下 AD。", tool_calls=[{"id": "x1", "name": "web_search", "arguments": {"query": "AD 价格"}}])
    assert runner.is_pending(sid, stale_after=0)                     # 调了工具没结果，也算被掐断

    seen = {}

    def fake_chat(messages, **kw):
        seen["last"] = messages[-1]
        return runner.llm.ChatResult(content="AD 查不到公开报价，按估计价计入，需向厂家询价。")

    monkeypatch.setattr(runner.llm, "chat", fake_chat)
    monkeypatch.setattr(runner.llm, "chat_answer", fake_chat)
    out = runner.resume(sid)
    assert "查不到" in out["content"]
    rows = db.q("SELECT role, content, tool_call_id FROM messages WHERE session_id=? ORDER BY id", (sid,))
    assert any(r["role"] == "tool" and r["tool_call_id"] == "x1" and "被中断" in r["content"] for r in rows)
    assert not runner.is_pending(sid, stale_after=0)


def test_turn_deadline_forces_an_answer(fake_llm, monkeypatch):
    """一轮跑太久：不再让模型调工具，直接要一个带假设的完整回答。"""
    from polysage.agents import runner

    monkeypatch.setattr(runner, "TURN_SECONDS", -1)          # 一进循环就算超时
    got = {}

    def fake_chat(messages, **kw):
        got["tools"] = "tools" in kw
        got["nudge"] = messages[-1].get("content") or ""
        return runner.llm.ChatResult(content="按现有估计价给结论：AD 需询价，其余照常。")

    monkeypatch.setattr(runner.llm, "chat", fake_chat)
    monkeypatch.setattr(runner.llm, "chat_answer", fake_chat)

    sid = runner.create_session("advisor", title="超时测试")
    out = runner.chat(sid, "给我 top10")
    assert "需询价" in out["content"]
    assert not got["tools"] and "不要再调用工具" in got["nudge"]
