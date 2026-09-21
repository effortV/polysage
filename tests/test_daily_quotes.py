"""贸易商日报：解析行 → 到厂价 → 入库 → 每类最低价写价格卡（不联网，假模型）。"""
from __future__ import annotations

from polysage import daily_quotes as D
from polysage import db, llm, pricing
from polysage.formulation import materials as MAT


def test_freight_and_ranks():
    assert D.freight_for("杭州") == 100 and D.freight_for("太仓") == 150 and D.freight_for("青岛") == 350
    assert D.freight_for("", delivery="配送绍兴") == D.freight_for("绍兴")
    assert D.freight_for("火星") == D.load_freight()["默认"]
    assert D.region_rank("上海") == 0 < D.region_rank("太仓") < D.region_rank("青岛")
    assert D.delivery_rank("码头现货") == 0 < D.delivery_rank("在途") < D.delivery_rank("本周计划") < D.delivery_rank("9月底")


def test_parse_save_and_apply(monkeypatch, home):
    MAT.seed_materials()

    def fake_json(messages, **kw):
        assert "报价商：贸易商B" in messages[-1]["content"]
        return {"quotes": [
            {"family": "LLDPE", "producer": "浙石化", "grade": "7042", "warehouse": "", "delivery": "本周计划", "price": 9650, "tax_included": True},
            {"family": "LLDPE", "producer": "华泰", "grade": "7042", "warehouse": "杭州", "delivery": "9月22起提货", "price": 9520, "tax_included": True},
            {"family": "LDPE", "producer": "浙石化", "grade": "2426H", "warehouse": "", "delivery": "下周计划", "price": 12000, "tax_included": True},
            {"family": "HDPE", "producer": "裕龙", "grade": "TR144", "warehouse": "", "delivery": "配送绍兴", "price": 9400, "tax_included": True},
            {"family": "HDPE", "producer": "塔里木", "grade": "5110FT", "warehouse": "金华", "delivery": "在途", "price": "9100", "tax_included": False},
            {"family": "其他", "producer": "x", "grade": "PP", "warehouse": "", "delivery": "", "price": 8000, "tax_included": True},
            {"family": "LLDPE", "producer": "坏", "grade": "?", "price": "abc"},
        ]}
    monkeypatch.setattr(llm, "chat_json", fake_json)

    res = D.import_text("……", "贸易商B", "2026-09-21", apply=True)
    rows = res["rows"]
    assert len(rows) == 6                                              # 非数字价格丢弃
    ht = next(r for r in rows if r["producer"] == "华泰")
    assert ht["freight"] == 100 and ht["landed"] == 9620                # 杭州运费 100
    tr = next(r for r in rows if r["grade"] == "TR144")
    assert tr["freight"] == D.freight_for("绍兴")                       # “配送绍兴”按绍兴算
    assert res["saved"] == {"new": 5, "dup": 0, "skip": 1}              # “其他”不进价格卡
    # 再导一次不重复
    assert D.import_text("……", "贸易商B", "2026-09-21", apply=False)["saved"]["dup"] == 5
    ps = D.picks("2026-09-21")
    assert ps["LLDPE"]["best"]["grade"] == "华泰 7042" and ps["LLDPE"]["best"]["landed_price"] == 9620
    assert ps["HDPE"]["best"]["grade"] == "塔里木 5110FT" and ps["HDPE"]["best"]["landed_price"] == 9300
    # 价格卡：LL 与 LLC 同价，LD、HD 各自最低
    cp = MAT.current_prices()
    assert cp["LL"]["actual"] == 9620 and cp["LLC"]["actual"] == 9620 and cp["LD"]["actual"] == 12150 and cp["HD"]["actual"] == 9300
    assert not D.apply_cheapest("2026-09-21")                           # 同日同价不重复写
    assert "华泰 7042" in D.picks_text("2026-09-21") and D.history("LLDPE")[-1]["landed"] == 9620
    for code in ("LL", "LLC", "LD", "HD"):
        pricing.undo_last_price(code, "actual")


def test_web_market_quotes_keeps_only_fresh_pages(monkeypatch, home):
    from datetime import date, timedelta

    from polysage.sources.base import SearchHit
    import polysage.sources.fetch as F
    import polysage.sources.websearch as W

    MAT.seed_materials()
    today = date.today()
    fresh = (today - timedelta(days=3)).isoformat()
    stale = (today - timedelta(days=40)).isoformat()
    pages = {
        "https://a.com/fresh": {"text": "浙石化7042 华东 厂提 9380 " * 30, "title": "PE日评", "date": fresh},
        "https://b.com/stale": {"text": "浙石化7042 华东 现货 7400 " * 30, "title": "旧文", "date": stale},
        "https://c.com/nodate": {"text": "宝来7042 杭州 现货 9500 " * 30, "title": "无日期", "date": ""},
    }
    monkeypatch.setattr(W, "search", lambda q, limit=8, **kw: [SearchHit(provider="bing", external_id=u, title=p["title"], source_type="web", url=u) for u, p in pages.items()])
    monkeypatch.setattr(F, "fetch_url", lambda url: dict(pages[url], kind="html", file_path=""))
    seen_urls = []

    def fake_json(messages, **kw):
        text = messages[-1]["content"]
        url = text.split("网页：", 1)[1].split("\n", 1)[0].strip()
        seen_urls.append(url)
        if "nodate" in url:
            return {"quotes": [{"family": "LLDPE", "producer": "宝来", "grade": "7042", "warehouse": "杭州", "delivery": "现货", "price": 9500, "tax_included": True, "seller": "某塑化", "date": ""}]}
        return {"quotes": [{"family": "LLDPE", "producer": "浙石化", "grade": "7042", "warehouse": "华东", "delivery": "厂提", "price": 9380, "tax_included": True, "seller": "生意社华东", "date": ""},
                           {"family": "HDPE", "producer": "镇海", "grade": "6098", "warehouse": "华东", "delivery": "现货", "price": 9600, "tax_included": True, "seller": "生意社华东", "date": ""}]}
    monkeypatch.setattr(llm, "chat_json", fake_json)

    s = D.web_market_quotes(["LLDPE"], depth="快", pages_per_query=3)["LLDPE"]
    assert s["pages"] == 2 and "https://b.com/stale" not in seen_urls          # 两周前的页面整页跳过
    assert s["rows"] == 1 and s["new"] == 1                                       # 无日期页面的报价丢弃；HDPE 行不算 LLDPE
    rows = D.web_rows("LLDPE")
    assert len(rows) == 1 and rows[0]["quote_date"] == fresh and rows[0]["channel"] == "网查" and rows[0]["trader"].startswith("生意社华东")
    assert rows[0]["landed_price"] == 9380 + D.freight_for("华东")
    applied = D.apply_cheapest()
    assert all(a["trader"] != rows[0]["trader"] for a in applied)               # 网查不进价格卡（只会用日报）
    for a in applied:                                                            # 还原，避免影响其他测试
        pricing.undo_last_price(a["code"], "actual")
    # 旧版泛报价（无 family）清理
    sid = D.sourcing.upsert_supplier({"name": "旧网查商家", "kind": "贸易商"})
    db.insert("supplier_quotes", {"supplier_id": sid, "material_code": "LL", "grade": "x", "price": 8000, "unit": "元/吨", "basis": "", "moq": "",
                                  "quote_date": "2026-09-01", "source_url": "u", "evidence": "", "credibility": 2, "status": "网查", "in_rfq": 0,
                                  "note": "", "created_at": db.now()})
    purged = D.purge_old_web_quotes()
    assert purged["quotes"] >= 1 and db.q1("SELECT id FROM suppliers WHERE name='旧网查商家'") is None
    assert len(D.web_rows("LLDPE")) == 1                                          # 新口径的网查报价保留
