"""Google Patents 不可达时的回退：搜索引擎 site: 结果解析、PubChem 专利 ID 与记录解析（不联网）。"""
from __future__ import annotations

from polysage.sources import patents, websearch
from polysage.sources.base import SearchHit


def test_search_via_web_parses_publication_numbers(monkeypatch):
    rows = [
        SearchHit(provider="duckduckgo", external_id="u1", title="CN102167855A - 茂金属聚乙烯棚膜树脂组合物 - Google Patents",
                  source_type="web", url="https://patents.google.com/patent/CN102167855A/zh", abstract="组合物中所述的茂金属..."),
        SearchHit(provider="duckduckgo", external_id="u2", title="三层共挤聚乙烯重包装膜 - Google Patents",
                  source_type="web", url="https://patents.google.com/patent/CN114454578", abstract="本发明属于..."),
        SearchHit(provider="duckduckgo", external_id="u3", title="重复", source_type="web",
                  url="https://patents.google.com/patent/CN114454578B/en", abstract=""),
        SearchHit(provider="duckduckgo", external_id="u4", title="无关页", source_type="web",
                  url="https://patents.google.com/?q=xx", abstract=""),
    ]
    seen = {}
    monkeypatch.setattr(websearch, "search", lambda q, limit=10, region="cn-zh", bing_first=False: seen.setdefault("q", q) and rows)
    monkeypatch.setattr(patents, "_blocked_until", float("inf"))   # 直连已判定不可达
    hits = patents.search_google_patents("茂金属 聚乙烯", limit=10)
    assert seen["q"].startswith("site:patents.google.com ")
    assert [h.external_id for h in hits] == ["CN102167855A", "CN114454578"]
    assert hits[0].title == "茂金属聚乙烯棚膜树脂组合物" and hits[0].source_type == "patent"
    assert hits[0].url.endswith("CN102167855A/en") and hits[0].meta["via"] == "web_search"


def test_pubchem_id_and_record_parsing(monkeypatch):
    assert patents.pubchem_patent_id("CN102167855A") == "CN-102167855-A"
    assert patents.pubchem_patent_id("US-7794806-B2") == "US-7794806-B2"
    assert patents.pubchem_patent_id("CN114454578") == "CN-114454578"
    record = {"Record": {"RecordTitle": "[Translated] Metallocene polyethylene film resin composition", "Section": [
        {"TOCHeading": "Abstract", "Information": [{"Value": {"StringWithMarkup": [{"String": "[Translated] A composition ..."}]}}]},
        {"TOCHeading": "Important Dates", "Section": [
            {"TOCHeading": "Publication Date", "Information": [{"Value": {"DateISO8601": ["2011-08-31"]}}]}]},
        {"TOCHeading": "Assignee", "Information": [{"Value": {"StringWithMarkup": [{"String": "PETROCHINA CO LTD"}]}}]},
    ]}}
    monkeypatch.setattr(patents, "get_json", lambda url, **kw: record)
    monkeypatch.setattr(patents, "_blocked_until", float("inf"))
    p = patents.fetch_google_patent("CN102167855A")
    assert p["source"] == "pubchem" and p["title"] == "Metallocene polyethylene film resin composition"
    assert p["abstract"] == "A composition ..." and p["assignee"] == "PETROCHINA CO LTD" and p["publication_date"] == "2011-08-31"
    assert p["claims"] == "" and "PubChem" in p["note"]
