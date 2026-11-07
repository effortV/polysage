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


ARTICLE = ("生意社11月06日讯\n11月6日，生意社LLDPE基准价为10350.00元/吨，与本月初(10275.00元/吨)相比，上涨了0.73%。\n"
           "LLDPE年度统计\n(2025-11-06--2026-11-06,元/吨)\n| 当日价 | 10350.00 | \n| 日涨幅 | -0.56% | \n| 一年位置 | 高位 |")


def test_parse_base_index():
    """生意社基准价日报：当日价、月初价、按日涨幅倒推的前一日价；标题（没有“与本月初”）、“与本月初持平”都认。"""
    import datetime as _dt

    today = _dt.date(2026, 11, 7)
    pts = {(p["family"], p["date"], p["kind"]): p["value"] for p in D.parse_base_index(ARTICLE, today)}
    assert pts[("LLDPE", "2026-11-06", "当日")] == 10350 and pts[("LLDPE", "2026-11-01", "月初")] == 10275
    assert abs(pts[("LLDPE", "2026-11-05", "前一日")] - 10350 / 0.9944) < 0.01
    flat = D.parse_base_index("11月2日生意社LDPE基准价为12766.67元/吨\n生意社11月02日讯11月2日,生意社LDPE基准价为12766.67元/吨,与本月初持平。"
                              "当日价12766.67日涨幅0.00%", today)
    kinds = {(p["family"], p["date"], p["kind"]): p["value"] for p in flat}
    assert kinds[("LDPE", "2026-11-02", "当日")] == 12766.67 and kinds[("LDPE", "2026-11-01", "月初")] == 12766.67
    assert not any(p["family"] == "LLDPE" for p in flat)                          # LDPE 不会认成 LLDPE
    assert D.parse_base_index("12月30日，生意社HDPE基准价为9000.00元/吨", today)[0]["date"] == "2025-12-30"   # 跨年
    assert D.parse_base_index("生意社2026年11月6日LLDPE趋势分析：温和回调", today) == []


def _east(fam, price, day, trader="区域行情日报", wh="华东", kind="市场价"):
    return {"family": fam, "producer": "", "grade": "膜料", "warehouse": wh, "delivery": "市场价", "price": price, "tax_included": True,
            "note": kind, "freight": 0, "landed": price, "trader": trader, "date": day, "url": "https://x.test/" + trader + day}


def test_market_nowcast_pushes_east_price_to_latest_index(home):
    """华东市场价停更：价格卡 = 最近一期华东市场价 × 生意社基准价的涨跌，日期是基准价的最新一天；日报新的时候还是用日报。"""
    import datetime as _dt

    MAT.seed_materials()
    db.execute("DELETE FROM supplier_quotes WHERE family='LDPE' AND channel='网查'")      # 前面测试留下的
    t = _dt.date.today()
    d = lambda n: (t - _dt.timedelta(days=n)).isoformat()                       # noqa: E731
    MAT.add_price("LD", 12150, "actual", source="贸易商日报（旧）", price_date=d(40))
    D.save_rows([_east("LDPE", 12000, d(5)), _east("LDPE", 12200, d(5), trader="另一家行情")], channel="网查", credibility=3)
    assert D.market_card_pick("LDPE")["how"] == "华东市场价"                       # 还没有基准价：照旧用华东市场价
    D.save_base_index([{"family": "LDPE", "date": d(6), "value": 13000, "kind": "月初"},
                       {"family": "LDPE", "date": d(1), "value": 13130, "kind": "当日"}])
    cp = D.market_card_pick("LDPE")
    assert cp["how"] == "推算" and cp["date"] == d(1) and cp["price"] == round(12100 * 1.01 / 10) * 10
    assert cp["nowcast"]["pct"] == 1.0 and "推到" in cp["text"]
    out = D.apply_web_market(["LDPE"])
    assert out[0]["applied"] and out[0]["date"] == d(1)
    eff = MAT.effective_prices()["LD"]
    assert eff["price"] == cp["price"] and eff["date"] == d(1) and eff["source"].startswith("网查行情推算")
    assert D.apply_web_market(["LDPE"]) and len([r for r in MAT.price_history("LD") if str(r["source"]).startswith("网查行情推算")]) == 1  # 不重复写
    MAT.add_price("LD", 12500, "actual", source="新日报", price_date=t.isoformat())   # 新日报来了就用日报
    assert MAT.effective_prices()["LD"]["price"] == 12500
    db.execute("DELETE FROM prices WHERE material_code='LD' AND (source IN ('贸易商日报（旧）', '新日报') OR source LIKE '网查行情%')")
    db.execute("DELETE FROM supplier_quotes WHERE family='LDPE' AND channel='网查'")
    db.execute("DELETE FROM price_index WHERE family='LDPE'")


def test_stale_east_price_is_not_replaced_by_national_benchmark(home):
    """华东市场价超过一周、又推不了（没有更新的基准价）：价格卡不动，不拿比成交价高 7% 的全国基准价顶上。"""
    import datetime as _dt

    MAT.seed_materials()
    db.execute("DELETE FROM supplier_quotes WHERE family='HDPE' AND channel='网查'")
    t = _dt.date.today()
    d = lambda n: (t - _dt.timedelta(days=n)).isoformat()                       # noqa: E731
    D.save_rows([_east("HDPE", 9360, d(10)), _east("HDPE", 11200, d(1), trader="生意社基准价", wh="全国", kind="基准价")],
                channel="网查", credibility=3)
    assert D.web_market_pick("HDPE")["tier"] == "全国市场价/基准价"               # 近一周只剩全国基准价
    before = MAT.effective_prices().get("HD")
    cp = D.market_card_pick("HDPE")
    assert cp["how"] == "不动" and cp["price"] is None and "价格卡先不动" in cp["text"]
    out = D.apply_web_market(["HDPE"])
    assert out[0]["applied"] is False and MAT.effective_prices().get("HD") == before
    D.save_base_index([{"family": "HDPE", "date": d(11), "value": 11000, "kind": "当日"},
                       {"family": "HDPE", "date": d(1), "value": 11200, "kind": "当日"}])
    cp = D.market_card_pick("HDPE")                                              # 有了基准价就推上来
    assert cp["how"] == "推算" and cp["price"] == round(9360 * 11200 / 11000 / 10) * 10 and cp["date"] == d(1)
    db.execute("DELETE FROM supplier_quotes WHERE family='HDPE' AND channel='网查'")
    db.execute("DELETE FROM price_index WHERE family='HDPE'")


def test_update_base_index_from_search(monkeypatch, home):
    """搜生意社基准价：摘要里有完整一句就直接取；标题是基准价、摘要截断了才读全文；不调模型。"""
    import polysage.sources.fetch as F
    import polysage.sources.websearch as W
    from polysage.sources.base import SearchHit

    t = __import__("datetime").date.today()
    md = f"{t.month}月{t.day}日"
    hits = [SearchHit(provider="bing", external_id="a", title=f"{md}生意社LLDPE基准价为10350.00元/吨", source_type="web",
                      url="https://goodsfu.test/a", abstract=f"生意社讯{md},生意社LLDPE基准价为10350.00元/吨,与本月初(10275.00元/吨)相比,上涨"),
            SearchHit(provider="bing", external_id="b", title=f"{md}生意社LLDPE基准价为10350.00元/吨", source_type="web",
                      url="https://goodsfu.test/b", abstract=f"{md},生意社LLDPE基准价为10350.00元/吨,与本月"),
            SearchHit(provider="bing", external_id="c", title="LLDPE 趋势分析", source_type="web", url="https://goodsfu.test/c", abstract="无关")]
    fetched = []
    monkeypatch.setattr(W, "search", lambda q, limit=8, **kw: hits)
    monkeypatch.setattr(F, "fetch_url", lambda url: fetched.append(url) or {"text": ARTICLE.replace("11月6日", md)})
    monkeypatch.setattr(llm, "chat_json", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该调模型")))
    out = D.update_base_index(["LLDPE"])
    assert out["LLDPE"]["last"] == t.isoformat() and out["LLDPE"]["value"] == 10350
    assert fetched == ["https://goodsfu.test/b"]                                 # 只有摘要截断的那篇读了全文
    s = D.base_series("LLDPE")
    if t.day > 1:
        assert s[t.replace(day=1).isoformat()] == 10275
    db.execute("DELETE FROM price_index WHERE family='LLDPE'")
