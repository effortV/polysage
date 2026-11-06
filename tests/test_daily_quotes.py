"""贸易商日报：解析行 → 到厂价 → 入库 → 每类最低价写价格卡（不联网，假模型）。"""
from __future__ import annotations

from polysage import daily_quotes as D
from polysage import db, llm, pricing
from polysage.formulation import materials as MAT

from _candidates import seed_candidates      # 测试候选材料池（正式库只预置 4 种基础料）


def test_freight_and_ranks():
    assert D.freight_for("杭州") == 100 and D.freight_for("太仓") == 150 and D.freight_for("青岛") == 350
    assert D.freight_for("", delivery="配送绍兴") == D.freight_for("绍兴")
    assert D.freight_for("火星") == D.load_freight()["默认"]
    assert D.region_rank("上海") == 0 < D.region_rank("太仓") < D.region_rank("青岛")
    assert D.delivery_rank("码头现货") == 0 < D.delivery_rank("在途") < D.delivery_rank("本周计划") < D.delivery_rank("9月底")


def test_parse_save_and_apply(monkeypatch, home):
    MAT.seed_materials()
    seed_candidates()

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
    seed_candidates()
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


def test_web_market_updates_price_card_when_daily_is_stale(home):
    """日报过期（>3 周）+ 有近一周网查行情 → 价格卡按网查中位算；日报新的时候还是用日报。"""
    import datetime as _dt

    from polysage import daily_quotes as DQ
    from polysage import db
    from polysage.aba.model import current_price_map
    from polysage.formulation import materials as MAT

    MAT.seed_materials()
    old = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
    MAT.add_price("LD", 12150, "actual", source="贸易商日报（旧）", price_date=old)
    today = _dt.date.today().isoformat()
    rows = [{"family": "LDPE", "producer": p, "grade": "2426H", "warehouse": wh, "delivery": "市场价", "price": price,
             "tax_included": True, "note": "", "freight": 0, "landed": price, "trader": tr, "date": today, "url": "https://example/x"}
            for p, wh, price, tr in (("万华", "华东", 13015, "生意社"), ("浙石化", "杭州", 12800, "某塑化"),
                                     ("扬子", "上海", 12900, "某贸易"), ("茂名", "广州", 12000, "华南某"))]
    DQ.save_rows(rows, channel="网查", credibility=3)
    out = DQ.apply_web_market(["LDPE"])
    # 只取华东的市场价 / 贸易商报价：万华自己的挂牌价（生意社转的厂家价）不算，华南的也不算
    assert out and out[0]["median"] == 12850 and out[0]["n"] == 2 and out[0]["tier"] == "华东市场价"
    assert out[0]["others"] == (12000, 13015, 2)
    eff = MAT.effective_prices()["LD"]
    assert eff["price"] == 12850 and eff["why"].startswith("网查价") and "未计入" in eff["source"]
    assert current_price_map()["LD"] == 12850
    MAT.add_price("LD", 12500, "actual", source="新日报", price_date=today)             # 新日报来了就用日报
    assert MAT.effective_prices()["LD"]["price"] == 12500 and current_price_map()["LD"] == 12500
    db.execute("DELETE FROM prices WHERE material_code='LD' AND source IN ('贸易商日报（旧）', '新日报')")
    db.execute("DELETE FROM prices WHERE material_code='LD' AND source LIKE '网查行情中位%'")


def test_quote_kinds_and_tiers(home):
    """网查行情分层：华东市场价优先；厂家挂牌价、期货公司评述只在没有别的时候用；同一来源连着几天只算最新一条。"""
    import datetime as _dt

    MAT.seed_materials()
    d0 = _dt.date.today().isoformat()
    d1 = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

    def row(price, wh, trader, kind, producer="", grade="", date=d0):
        return {"family": "HDPE", "producer": producer, "grade": grade, "warehouse": wh, "delivery": "市场价", "price": price,
                "tax_included": True, "note": kind, "freight": 0, "landed": price, "trader": trader, "date": date, "url": "https://x.test/" + trader}

    D.save_rows([row(11000, "华东", "生意社(toutiao.toocle.com)", "厂家", "万华", "7000F"),
                 row(10421, "", "光大期货(goodsfu.10jqka.com.cn)", "评述")], channel="网查", credibility=3)
    p = D.web_market_pick("HDPE")
    assert p["tier"].startswith("厂家") and p["n"] == 2                                  # 只有这两种
    before = MAT.effective_prices().get("HD")
    out = D.apply_web_market(["HDPE"])
    assert out and out[0]["applied"] is False and MAT.effective_prices().get("HD") == before   # 挂牌价/评述不写价格卡
    D.save_rows([row(9360, "华东", "区域行情日报", "市场价"), row(9300, "华东", "区域行情日报", "市场价", date=d1),
                 row(9180, "华北", "区域行情日报", "市场价")], channel="网查", credibility=3)
    p = D.web_market_pick("HDPE")
    assert p["tier"] == "华东市场价" and p["n"] == 1 and p["median"] == 9360               # 同一来源前一天的那条不重复算
    assert p["others"][2] == 3 and p["who"] == "区域行情日报"
    assert D.quote_kind({"trader": "光大期货(goodsfu.10jqka.com.cn)"}) == "评述"
    assert D.quote_kind({"trader": "生意社(toutiao.toocle.com)", "producer": "万华"}) == "厂家"
    assert D.quote_kind({"trader": "生意社基准价(goodsfu.10jqka.com.cn)"}) == "基准价"
    assert D.quote_kind({"basis": "含税(H) 贸易商", "trader": "宁波某塑化"}) == "贸易商"
    assert D.short_source("光大期货(goodsfu.10jqka.com.cn)") == "光大期货" and D.short_source("某塑化(有限公司)") == "某塑化(有限公司)"
    db.execute("DELETE FROM supplier_quotes WHERE family='HDPE' AND channel='网查'")


def test_market_feed_pages(monkeypatch, home):
    """区域行情日报：读 RSS，只要标题里带日期、近一周的那篇。"""
    import datetime as _dt

    import polysage.sources.base as B
    import polysage.sources.fetch as F

    today = _dt.date.today()
    new, old = today - _dt.timedelta(days=2), today - _dt.timedelta(days=30)

    def title(d):
        return f"今日中国新塑料行情：{d.year}年{d.month:02d}月{d.day:02d}日主要区域报价平稳"

    rss = (f"<rss><channel><item><title>{title(new)}</title><link>https://feed.test/a</link></item>"
           f"<item><title>别的新闻</title><link>https://feed.test/x</link></item>"
           f"<item><title>{title(old)}</title><link>https://feed.test/b</link></item></channel></rss>")
    monkeypatch.setattr(D, "MARKET_FEEDS", [("https://feed.test/rss", "新塑料行情")])
    monkeypatch.setattr(B, "get_bytes", lambda url, *a, **kw: ("application/rss+xml; charset=utf-8", rss.encode("utf-8")))
    monkeypatch.setattr(F, "fetch_url", lambda url: {"text": "华东 PE · LLDPE 膜料 9,630 元/吨｜含税参考价 " * 20})
    pages = D.market_feed_pages(today)
    assert [(u, d) for u, _, d in pages] == [("https://feed.test/a", new.isoformat())]
    assert pages[0][1].startswith("今日中国新塑料行情")
    monkeypatch.setattr(B, "get_bytes", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("断网")))
    assert D.market_feed_pages(today) == []                                             # 读不到不报错


def test_web_market_film_grades_only(home):
    """区域行情表里同一类有膜料、注塑、拉丝……只拿膜料的价。"""
    import datetime as _dt

    MAT.seed_materials()
    d0 = _dt.date.today().isoformat()
    rows = [{"family": "HDPE", "producer": "", "grade": g, "warehouse": "华东", "delivery": "市场价", "price": p, "tax_included": True,
             "note": "市场价", "freight": 0, "landed": p, "trader": "区域行情日报", "date": d0, "url": "https://x.test/f"}
            for g, p in (("膜料", 9360), ("注塑", 9760), ("拉丝", 10350))]
    D.save_rows(rows, channel="网查", credibility=3)
    p = D.web_market_pick("HDPE")
    assert p["median"] == 9360 and p["n"] == 1
    db.execute("DELETE FROM supplier_quotes WHERE family='HDPE' AND channel='网查'")
