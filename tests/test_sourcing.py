"""供应商寻源：检索词、抽取入库、比较、询价清单、登记实价（不联网，假搜索/假抓取/假 LLM）。"""
from __future__ import annotations

import pytest

from polysage import db, llm, pricing, sourcing
from polysage.formulation import materials as MAT
from polysage.sources.base import SearchHit


@pytest.fixture
def fake_net(monkeypatch):
    MAT.seed_materials()
    hits = [SearchHit(provider="duckduckgo", external_id=f"u{i}", title=f"LLDPE 7042 报价 {i}", source_type="web",
                      url=f"https://www.1688.com/offer/{i}.html", abstract="") for i in range(3)]
    monkeypatch.setattr(sourcing, "web_search", lambda q, limit=8, bing_first=False: hits)
    monkeypatch.setattr(sourcing, "fetch_url", lambda url: {"text": "某某塑化 LLDPE 7042 现货 8350 元/吨 含税 " * 20, "file_path": "", "title": "报价页"})

    def fake_json(messages, **kw):
        text = messages[-1]["content"]
        assert "offers" in text
        return {"offers": [
            {"supplier": "杭州某某塑化有限公司", "kind": "贸易商", "region": "浙江杭州", "grade": "7042", "price_rmb_per_ton": 8350,
             "basis": "含税 现货", "moq": "1 吨", "date": "2026-09", "contact": "0571-xxxx", "evidence": "7042 现货 8350 元/吨"},
            {"supplier": "生意社 华东市场", "kind": "行情", "region": "华东", "grade": "7042", "price_rmb_per_ton": 8420,
             "basis": "市场价", "moq": "", "date": "2026-09-17", "contact": "", "evidence": "华东 8420"},
            {"supplier": "无价格商家", "kind": "贸易商", "price_rmb_per_ton": None},
            {"supplier": "单位是公斤", "kind": "平台商家", "price_rmb_per_ton": 8.6, "basis": "平台标价"},
        ]}
    monkeypatch.setattr(llm, "chat_json", fake_json)
    return hits


def test_queries_and_keyword():
    m = MAT.get_material("LLC") or {"code": "LLC", "name": "国产 C4 LLDPE（7042 类）", "grade": "7042 / 218W"}
    assert sourcing.keyword(m) == "LLDPE 7042"
    qs = sourcing.queries_for(m, "快")
    assert len(qs) == 2 and qs[0].startswith("LLDPE 7042")
    r1 = {"code": "R1", "name": "再生高压一级透明料", "grade": "—", "is_recycled": 1}
    assert sourcing.keyword(r1) == "再生高压一级透明料"
    assert any("再生" in q for q in sourcing.queries_for(r1, "标准"))
    assert sourcing.queries_for(m, "标准", extra="华东")[1] == "LLDPE 7042 华东"


def test_run_compare_rfq_and_register(fake_net, home):
    before = {q["id"] for q in sourcing.quotes("LLC", include_untrusted=True)}
    summary = sourcing.run(["LLC"], depth="快")
    s = summary["LLC"]
    assert s["n_offers"] >= 3 and s["n_suppliers"] >= 2                    # 抽到 3 条有效报价（无价格的丢弃，元/kg 换算）
    rows = sourcing.compare("LLC")
    assert rows and rows[0]["price"] == 8350 and rows[0]["supplier"] == "杭州某某塑化有限公司"
    assert any(r["kind"] == "行情" for r in rows)
    kg = next(r for r in rows if r["supplier"] == "单位是公斤")
    assert kg["price"] == 8600 and kg["basis_flag"] is True                 # 8.6 元/kg → 8600；平台标价标为口径存疑
    assert rows[0]["current"] is not None and rows[0]["diff"] is not None
    assert sourcing.REPORT_PATH.exists() and "7042" in sourcing.REPORT_PATH.read_text(encoding="utf-8")
    # 再跑一次不重复入库
    sourcing.run(["LLC"], depth="快")
    ids = [q["id"] for q in sourcing.quotes("LLC", include_untrusted=True)]
    assert len(ids) == len(set(ids)) and len(ids) - len(before) == 3
    # 询价清单
    qid = rows[0]["id"]
    sourcing.set_quote(qid, in_rfq=1, status="询价中")
    assert [q["id"] for q in sourcing.rfq_rows()] == [qid]
    path = sourcing.export_rfq(home / "rfq_test.xlsx")
    assert path.exists() and path.stat().st_size > 1000
    # 登记实价 → 价格卡出现实价，报价状态已登记
    res = sourcing.register_actual(qid, 8300, note="含税到厂")
    assert MAT.current_prices()["LLC"]["actual"] == 8300
    q = db.q1("SELECT * FROM supplier_quotes WHERE id=?", (qid,))
    assert q["status"] == "已登记" and q["actual_price"] == 8300
    assert isinstance(res, dict)
    pricing.undo_last_price("LLC", "actual")   # 还原，避免影响其他测试


def test_untrusted_hidden_and_supplier_upsert(fake_net):
    sid = sourcing.upsert_supplier({"name": "  测试厂商 ", "kind": "生产商", "region": "江苏"})
    assert sourcing.upsert_supplier({"name": "测试厂商", "contact": "138"}) == sid
    row = db.q1("SELECT * FROM suppliers WHERE id=?", (sid,))
    assert row["kind"] == "生产商" and row["contact"] == "138"
    qid = sourcing.add_quote("LL", {"supplier": "测试厂商", "kind": "生产商", "price": 9000, "basis": "含税", "grade": "", "moq": "",
                                    "date": "", "contact": "", "evidence": "手动", "region": ""}, "手动录入", 5)
    assert qid and any(r["id"] == qid for r in sourcing.compare("LL"))
    sourcing.set_quote(qid, status="不可信")
    assert not any(r["id"] == qid for r in sourcing.compare("LL"))
    assert any(q["id"] == qid for q in sourcing.quotes("LL", include_untrusted=True))
    sourcing.delete_supplier(sid)
    assert db.q1("SELECT id FROM supplier_quotes WHERE id=?", (qid,)) is None   # 级联删除
