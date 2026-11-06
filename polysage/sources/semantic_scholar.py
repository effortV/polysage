"""Semantic Scholar Graph API（免费；无 key 时限速约 1 次/秒）。"""
from __future__ import annotations

from typing import Any

from ..config import settings
from .base import SearchHit, get_json, normalize_doi

API = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,abstract,year,venue,authors,externalIds,openAccessPdf,citationCount,url"


def _hit(p: dict[str, Any]) -> SearchHit:
    ext = p.get("externalIds") or {}
    doi = normalize_doi(ext.get("DOI") or "")
    oa = (p.get("openAccessPdf") or {}).get("url") or ""
    return SearchHit(
        provider="semantic_scholar",
        external_id=p.get("paperId") or "",
        title=p.get("title") or "",
        source_type="paper",
        authors=", ".join(a.get("name", "") for a in (p.get("authors") or [])[:6]),
        year=p.get("year"),
        venue=p.get("venue") or "",
        doi=doi,
        url=(f"https://doi.org/{doi}" if doi else (p.get("url") or "")),
        abstract=p.get("abstract") or "",
        oa_pdf_url=oa,
        credibility=3,
        meta={"citationCount": p.get("citationCount")},
    )


def search(query: str, limit: int = 20, year_from: int | None = None) -> list[SearchHit]:
    params: dict[str, Any] = {"query": query, "limit": min(limit, 100), "fields": FIELDS}
    if year_from:
        params["year"] = f"{year_from}-"
    headers = {}
    if settings.semantic_scholar_api_key:
        headers["x-api-key"] = settings.semantic_scholar_api_key
    data = get_json(API, params=params, headers=headers) or {}
    return [_hit(p) for p in data.get("data") or []]


def ping(key: str | None = None) -> tuple[bool, str]:
    """设置页“测试 Semantic Scholar”：带 key 搜一条；不带 key 也能用，但容易被限流。"""
    import httpx

    from .. import net

    key = key if key is not None else settings.semantic_scholar_api_key
    headers = {"User-Agent": settings.user_agent}
    if key:
        headers["x-api-key"] = key
    try:
        with net.client(API, timeout=30.0) as c:
            r = c.get(API, params={"query": "LLDPE blown film tensile", "limit": 1, "fields": "title,year"}, headers=headers)
    except httpx.HTTPError as e:
        return False, f"连不上 api.semanticscholar.org：{e}"
    if r.status_code == 200:
        data = r.json()
        first = ((data.get("data") or [{}])[0]).get("title", "")
        return True, (f"可用{'（带 key）' if key else '（没填 key，公共额度，容易限流）'}：测试检索共 {data.get('total', 0)} 篇，"
                      f"第一篇《{first[:60]}》")
    if r.status_code in (401, 403):
        return False, f"{r.status_code}：key 无效或没开通这个接口，检查是否复制完整。"
    if r.status_code == 429:
        return False, "429：请求太频繁（没 key 时常见），过一会再测。"
    return False, f"异常 {r.status_code}：{r.text[:200]}"
