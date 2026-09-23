"""采购方案：来源优先级（实价 > 日报 > 网查 > 估计价）、用量与小计、导出、辅料网查入库。"""
from __future__ import annotations

from datetime import date, timedelta

from polysage import daily_quotes as D
from polysage import db, llm, pricing, procurement as P
from polysage.formulation import materials as MAT
from polysage.sources.base import SearchHit


def _clear_actuals(*codes: str) -> None:
    """其他测试可能给这些料留下实价，采购方案的来源优先级测试需要干净起点。"""
    for code in codes:
        while MAT.current_prices().get(code, {}).get("actual") is not None:
            pricing.undo_last_price(code, "actual")


def test_best_source_priority_and_plan(monkeypatch, home):
    MAT.seed_materials()
    _clear_actuals("LL", "LLC", "LD", "HD", "R1", "AD", "POE")
    today = date.today().isoformat()
    # 日报：LL/LLC 9670；辅料 R1 网查 7600
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"quotes": [
        {"family": "LLDPE", "producer": "华泰", "grade": "7042", "warehouse": "杭州", "delivery": "现货", "price": 9520, "tax_included": True}]})
    D.import_text("……", "贸易商B", today, apply=False)
    sid = D.sourcing.upsert_supplier({"name": "台州某某再生", "kind": "回收厂", "region": "浙江台州", "contact": "139-xxxx"})
    db.insert("supplier_quotes", {"supplier_id": sid, "material_code": "R1", "grade": "一级透明", "price": 7400, "unit": "元/吨",
                                  "basis": "含税", "moq": "1 吨", "quote_date": today, "source_url": "https://x/1", "evidence": "7400",
                                  "credibility": 3, "status": "网查", "in_rfq": 0, "note": "", "created_at": db.now(),
                                  "family": None, "producer": "", "warehouse": "浙江台州", "delivery": "", "landed_price": 7600, "channel": "网查"})

    s_ll = P.best_source("LL")
    assert s_ll["tier"] == "daily" and s_ll["price"] == 9520 + D.freight_for("杭州") and s_ll["supplier"] == "贸易商B"
    s_r1 = P.best_source("R1")
    assert s_r1["tier"] == "web" and s_r1["price"] == 7600 and s_r1["contact"] == "139-xxxx" and s_r1["region"] == "浙江台州"
    assert P.best_source("AD")["tier"] == "estimate"            # 只有估计价，但必配助剂仍算可采购
    pricing.record_price("R1", 7300, "actual", supplier="台州某某再生", source="询价")
    assert P.best_source("R1")["tier"] == "actual" and P.best_source("R1")["price"] == 7300
    pricing.undo_last_price("R1", "actual")
    assert MAT.current_prices().get("R1", {}).get("actual") is None                 # 还原，避免影响其他测试

    plan = P.plan({"LL": 35, "R1": 44, "RL": 20, "AD": 1}, tons=2)
    assert [it["code"] for it in plan["items"]] == ["R1", "LL", "RL", "AD"]      # 比例从高到低
    r1 = plan["items"][0]
    assert r1["qty_t"] == 0.88 and r1["subtotal"] == round(7600 * 0.88)          # 2 吨 × 44%
    assert plan["total"] and plan["cost_per_ton"] == round(plan["total"] / 2) and plan["missing"] == []
    txt = P.text(plan)
    assert "台州某某再生" in txt and "139-xxxx" in txt and "元/吨" in txt
    path = P.export(plan, home / "采购清单_test.xlsx")
    assert path.exists() and path.stat().st_size > 2000


def test_missing_price_is_flagged(monkeypatch, home):
    MAT.seed_materials()
    MAT.upsert_material({"code": "ZZZ", "name": "没价格的料", "category": "功能助剂", "active": 1})
    plan = P.plan({"LL": 99, "ZZZ": 1})
    assert plan["missing"] == ["ZZZ"] and plan["total"] is None
    assert "待询价" in P.text(plan)


def test_material_web_sourcing(monkeypatch, home):
    MAT.seed_materials()
    import polysage.sources.fetch as F
    import polysage.sources.websearch as W

    fresh = (date.today() - timedelta(days=5)).isoformat()
    monkeypatch.setattr(W, "search", lambda q, limit=8, **kw: [SearchHit(provider="bing", external_id="u", title="再生料报价", source_type="web", url="https://a.com/x")])
    monkeypatch.setattr(F, "fetch_url", lambda url: {"text": "一级透明再生颗粒 7400 元/吨 " * 30, "title": "报价", "date": fresh, "kind": "html", "file_path": ""})
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"quotes": [
        {"supplier": "宁波某某再生塑料", "kind": "回收厂", "region": "宁波", "grade": "一级透明", "price": 7400, "tax_included": True,
         "moq": "1 吨", "contact": "0574-xxxx", "date": "", "evidence": "一级透明 7400"},
        {"supplier": "供应商 7", "kind": "贸易商", "price": 7000},                      # 匿名，丢弃
        {"supplier": "某某化工", "kind": "贸易商", "price": 7.2},                        # 元/kg → 7200
    ]})
    _clear_actuals("R1")
    s = D.web_material_quotes(["R1"], depth="快", pages_per_query=1)["R1"]
    assert s["rows"] == 2 and s["new"] >= 1
    rows = D.material_rows("R1")
    assert rows[0]["landed_price"] == 7200 + D.freight_for("")                     # 元/kg 换算后最便宜
    assert any(r["contact"] == "0574-xxxx" for r in rows)
    D.apply_web_estimates(["R1"])
    assert MAT.current_prices()["R1"]["estimate"] == rows[0]["landed_price"]
    assert P.best_source("R1")["tier"] == "web"
    pricing.undo_last_price("R1", "estimate")


def test_material_extraction_accepts_market_tables(monkeypatch, home):
    """行情价格表（没有商家名）按 kind=行情 收下；商家页按元/kg 换算。"""
    from datetime import date

    MAT.seed_materials()
    today = date.today()
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"quotes": [
        {"supplier": "山东再生PE市场", "kind": "行情", "region": "山东", "grade": "EVA白色透明一级颗粒", "price": 5300, "tax_included": True,
         "moq": "", "contact": "", "date": "", "evidence": "一级颗粒5300"},
        {"supplier": "青岛聚利新能源科技有限公司", "kind": "生产商", "region": "山东", "grade": "高压一级再生", "price": 7300,
         "tax_included": False, "moq": "1 吨", "contact": "0532-x", "date": "", "evidence": "7.30 元/千克"},
        {"supplier": "山东", "kind": "贸易商", "price": 6000},          # 纯地区名 + 非行情 → 丢
    ]})
    rows = D.extract_material_quotes("R1", "https://x/1", "正文" * 200, today, today.isoformat())
    assert [r["supplier"] for r in rows] == ["山东再生PE市场", "青岛聚利新能源科技有限公司"]
    assert rows[0]["kind"] == "行情" and rows[1]["contact"] == "0532-x"
    assert all(r["date"] == today.isoformat() for r in rows)          # 页面没写日期时按当天记


def test_quality_filter_drops_unusable_grades(monkeypatch, home):
    """便宜但用不了的料（大棚膜/发黄/杂色/破碎/注塑）不进库。"""
    from datetime import date

    MAT.seed_materials()
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"quotes": [
        {"supplier": "东北再生高压市场", "kind": "行情", "region": "东北", "grade": "白色略发黄大棚膜一级颗粒", "price": 4400, "evidence": "大棚膜料 4400"},
        {"supplier": "广东再生高压市场", "kind": "行情", "region": "广东", "grade": "高压白透明一级造粒", "price": 6300, "evidence": "白透明一级 6300"},
        {"supplier": "某某再生厂", "kind": "回收厂", "region": "山东", "grade": "杂色瓶盖破碎料", "price": 4450, "evidence": "破碎 4450"},
    ]})
    rows = D.extract_material_quotes("R1", "https://x/1", "正文" * 200, date.today(), "")
    assert [r["supplier"] for r in rows] == ["广东再生高压市场"] and rows[0]["price"] == 6300
    assert D._quality_ok("POE", "8150", "")          # 没配过滤词的材料不受影响


def test_library_carries_sourcing_and_recommend_uses_buyable(monkeypatch, home):
    """配方库带“原料采购”栏并随报价更新；推荐只用买得到的料。"""
    from datetime import date

    from polysage.formulation import library as L

    MAT.seed_materials()
    _clear_actuals("LL", "LLC", "R1", "AD")
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"quotes": [
        {"family": "LLDPE", "producer": "华泰", "grade": "7042", "warehouse": "杭州", "delivery": "现货", "price": 9520, "tax_included": True}]})
    D.import_text("……", "贸易商B", date.today().isoformat(), apply=False)

    L.save_formulation("T01", {"LL": 60, "LD": 10, "HD": 5, "R1": 25}, rationale="测试", origin="测试")   # 现配方的料都算买得到
    f = next(x for x in L.list_formulations() if x["code"] == "T01")
    assert "LL" in (f["sourcing_note"] or "") and "日报" in f["sourcing_note"]
    assert P.is_buyable("LL") and P.is_buyable("AD")          # AD 是必配助剂，视为可采购（价格待询价）

    # 给 R1 一条网查报价 → 配方库的“原料采购”栏自动更新
    sid = D.sourcing.upsert_supplier({"name": "东莞某某再生", "kind": "回收厂", "region": "广东东莞"})
    D.save_material_rows([{"code": "R1", "supplier": "东莞某某再生", "kind": "回收厂", "region": "广东东莞", "grade": "高压一级透明",
                           "price": 6000, "tax_included": True, "moq": "1 吨", "contact": "0769-x", "date": date.today().isoformat(),
                           "freight": 400, "landed": 6400, "evidence": "6000", "url": "https://x/2", "host": "x"}])
    f = next(x for x in L.list_formulations() if x["code"] == "T01")
    assert "东莞某某再生" in f["sourcing_note"] and "6400" in f["sourcing_note"] and f["buyable"]
    assert sid and P.is_buyable("R1")

    csv_text = L.export_library(home / "lib.csv").read_text(encoding="utf-8-sig")
    assert "原料采购（怎么来的）" in csv_text and "全部可采购" in csv_text


def test_library_rejects_unbuyable_formulations(monkeypatch, home):
    """买不到的配方不进配方库；现配方在用的料算买得到。"""
    from polysage.formulation import constraints as C
    from polysage.formulation import library as L

    MAT.seed_materials()
    _clear_actuals("LL", "LLC", "R1", "AD", "POE")
    for f in L.list_formulations():                      # 其他测试留下的配方不影响本例
        if f["code"] in ("OK1", "OK2", "BAD1"):
            db.delete("formulations", f["id"])
    base = C.base_formulation()
    assert all(P.is_buyable(c) for c in base)              # 现配方的料：工厂本来就在买
    assert P.is_buyable("AD")                              # 必配助剂：同样视为可采购（价格待询价）
    assert not P.is_buyable("POE")                         # 只有内部估计价的新料：买不到，不能进方案

    with __import__("pytest").raises(L.NotPurchasable) as e:
        L.save_formulation("BAD1", {"LL": 60, "R1": 34, "POE": 5, "AD": 1}, origin="测试")
    assert "POE" in str(e.value)
    assert not any(f["code"] == "BAD1" for f in L.list_formulations())

    ok = L.save_formulation("OK1", dict(base), origin="测试")
    assert ok and any(f["code"] == "OK1" for f in L.list_formulations())

    # 给 POE 一条网查报价后就能入库
    D.save_material_rows([{"code": "POE", "supplier": "上海某某塑化", "kind": "贸易商", "region": "上海", "grade": "8150",
                           "price": 14700, "tax_included": True, "moq": "", "contact": "021-x", "date": __import__("datetime").date.today().isoformat(),
                           "freight": 100, "landed": 14800, "evidence": "14700", "url": "https://x/poe", "host": "x"}])
    assert P.is_buyable("POE")
    assert L.save_formulation("OK2", {"LL": 60, "R1": 34, "POE": 5, "AD": 1}, origin="测试")

    # 来源失效后可以把买不到的旧配方清出去
    monkeypatch.setattr(P, "unbuyable", lambda comps: ["POE"] if "POE" in comps else [])
    removed = L.purge_unbuyable()
    assert "OK2" in removed and not any(f["code"] == "OK2" for f in L.list_formulations())
