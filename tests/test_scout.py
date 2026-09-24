"""原料库来源：只预置现配方在用的 4 种料，其余靠智能体上网发现（带来源网址）。"""
from __future__ import annotations

from polysage import db
from polysage.formulation import materials as MAT
from polysage.sources.base import SearchHit


def test_seed_only_base_materials(home):
    """种子里只有现配方在用的 4 种，且不再写入附录 D 假设价。"""
    seeded = [m["code"] for m in MAT.SEED_MATERIALS]
    assert seeded == ["LL", "LD", "HD", "R1"] == list(MAT.BASE_CODES)
    assert [m["price"] for m in MAT.SEED_MATERIALS] == [12000, None, None, 8000]      # 只留甲方口述价
    MAT.seed_materials()
    assert {c for c in seeded} <= {m["code"] for m in MAT.list_materials(active_only=False)}
    assert not db.q("SELECT id FROM prices WHERE COALESCE(source,'') || COALESCE(note,'') LIKE ?",
                    (f"%{MAT.ASSUMED_MARK}%", ))                                       # 库里不该再有附录 D 的价


def test_purge_assumed_keeps_evidenced(home):
    """清理：只靠附录 D 假设价活着的料删掉，有报价佐证的留下，现配方 4 种料永远留。"""
    MAT.seed_materials()
    MAT.upsert_material({"code": "FAKE", "name": "虚构料", "category": "助剂"})
    MAT.add_price("FAKE", 60000, "estimate", source=MAT.ASSUMED_MARK + " 假设价（演示用）")
    MAT.upsert_material({"code": "REAL", "name": "有据料", "category": "主体树脂"})
    MAT.add_price("REAL", 9000, "estimate", source="网查参考价：某某塑化", url="https://example.com/a")
    MAT.upsert_material({"code": "FOUND", "name": "智能体找到的料", "category": "再生料",
                         "origin": "智能体发现 再生 PE 颗粒", "source_url": "https://example.com/b"})

    res = MAT.purge_assumed()
    left = {m["code"] for m in MAT.list_materials(active_only=False)}
    assert "FAKE" not in left and "FAKE" in res["removed"]
    assert {"LL", "LD", "HD", "R1", "REAL", "FOUND"} <= left
    assert res["prices_deleted"] == 1
    assert not db.q("SELECT id FROM prices WHERE material_code='FAKE'")


def test_discover_adds_material_with_source(monkeypatch, home):
    """发现新料：搜到页面 → 抽出牌号与价格 → 入库并带来源网址，价格写成估计价。"""
    from polysage import llm, scout

    MAT.seed_materials()
    monkeypatch.setattr("polysage.sources.websearch.search",
                        lambda q, **kw: [SearchHit(provider="bing", external_id="x1", title="星海塑化 XH-9001 报价",
                                                   source_type="web", url="https://example.com/q", abstract="", credibility=3)])
    monkeypatch.setattr("polysage.sources.fetch.fetch_url",
                        lambda url, **kw: {"title": "星海塑化 XH-9001 报价", "text": "星海牌吹膜专用 PE XH-9001 含税 8600 元/吨。" * 20, "date": "2026-09-20"})
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"materials": [
        {"name": "星海牌吹膜专用 PE", "category": "主体树脂", "grade": "XH-9001", "producer": "星海塑化", "is_recycled": 0,
         "role": "替代现用 LLDPE，价格更低", "typical_min": 10, "typical_max": 60,
         "effects": {"拉伸": "≈", "撕裂": "≈", "穿刺": "≈", "热封": "≈"}, "risk": "批次波动",
         "price": 8600, "price_basis": "含税 8600 元/吨"},
        {"name": "实验室自制共聚物", "category": "主体树脂", "grade": "", "price": 999999}]})

    out = scout.discover("星海牌吹膜专用 PE", depth="快", max_new=3)
    assert len(out["added"]) == 1
    new = out["added"][0]
    m = MAT.get_material(new["code"])
    assert m["source_url"] == "https://example.com/q" and "智能体发现" in m["origin"] and m["use_flag"] == "待评估"
    assert MAT.current_prices()[new["code"]]["estimate"] == 8600          # 网页上的价写成估计价
    assert scout.discovered()[0]["code"] == new["code"]

    again = scout.discover("星海牌吹膜专用 PE", depth="快", max_new=3)   # 同一种料不重复入库
    assert not again["added"]


def test_same_price_for_several_materials_is_dropped(monkeypatch, home):
    """一个页面里几种料报同一个价：那是页面通用价，不能当成每种料的报价。"""
    from polysage import llm, scout
    from polysage.formulation import materials as MAT

    monkeypatch.setattr("polysage.sources.websearch.search",
                        lambda q, **kw: [SearchHit(provider="bing", external_id="y1", title="某平台塑料报价",
                                                   source_type="web", url="https://example.com/list", abstract="", credibility=3)])
    monkeypatch.setattr("polysage.sources.fetch.fetch_url",
                        lambda url, **kw: {"title": "某平台塑料报价", "text": "各类聚乙烯现货 9900 元/吨起。" * 30, "date": "2026-09-20"})
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"materials": [
        {"name": "昊天 LLDPE", "category": "主体树脂", "grade": "HT-101", "producer": "昊天", "price": 9900},
        {"name": "昊天 LDPE", "category": "主体树脂", "grade": "HT-202", "producer": "昊天", "price": 9900},
        {"name": "昊天 HDPE", "category": "主体树脂", "grade": "HT-303", "producer": "昊天", "price": 9900}]})

    out = scout.discover("昊天 聚乙烯", depth="快", max_new=5)
    assert len(out["added"]) == 3                                   # 料照样入库（待评估）
    assert all(a["price"] is None for a in out["added"])            # 但这个“通用价”不采用
    for a in out["added"]:
        assert MAT.current_prices().get(a["code"], {}).get("estimate") is None
    assert any("同一个价" in n for n in out["notes"])


def test_price_sanity_range_by_category():
    """行情表里数字连在一起会抽出 86000 元/吨的 LLDPE：按类别的常识区间挡掉。"""
    from polysage.scout import _clean

    def price(cat, v, **kw):
        out = _clean({"name": "某料", "category": cat, "grade": "X1", "price": v, **kw})
        return (out or {}).get("price")

    assert price("主体树脂", 86000) is None and price("主体树脂", 8600) == 8600      # 树脂不可能 8.6 万
    assert price("再生料", 6100) == 6100 and price("再生料", 61000) is None
    assert price("助剂", 60000) == 60000                                            # 助剂本来就贵，别误杀


def test_black_recycled_is_flagged_unusable(monkeypatch, home):
    """黑色/杂色回料再便宜也不能进包装膜：发现时标“不用”，配方生成也不碰它。"""
    from polysage import llm, scout
    from polysage.formulation import generator as G, materials as MAT

    monkeypatch.setattr("polysage.sources.websearch.search",
                        lambda q, **kw: [SearchHit(provider="bing", external_id="z1", title="再生PE市场价格表",
                                                   source_type="web", url="https://example.com/rec", abstract="", credibility=3)])
    monkeypatch.setattr("polysage.sources.fetch.fetch_url",
                        lambda url, **kw: {"title": "再生PE市场价格表", "text": "黑色一级颗粒 4000 元/吨。" * 30, "date": "2026-09-20"})
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {"materials": [
        {"name": "再生高压黑色一级颗粒", "category": "再生料", "grade": "", "producer": "某再生厂", "is_recycled": 1,
         "role": "成本最低的回料", "typical_min": 10, "typical_max": 40, "price": 4000, "price_basis": "4000 元/吨"}]})

    out = scout.discover("再生 PE 颗粒", depth="快", max_new=2)
    assert len(out["added"]) == 1
    m = MAT.get_material(out["added"][0]["code"])
    assert m["use_flag"].startswith("否") and "外观" in m["use_flag"]
    themes = G.generate(n_out=5, n_samples=800, seed=3, cost_limit=1e9)
    assert all(m["code"] not in c.components for c in themes)        # 不会被采样进配方
