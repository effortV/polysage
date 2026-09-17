"""OpenAlex 文献检索（https://docs.openalex.org）。

- 支持 api_key（.env 的 OPENALEX_API_KEY）与 mailto 礼貌池。
- 摘要以倒排索引形式返回，这里还原为文本。
"""
from __future__ import annotations

from typing import Any

from ..config import settings
from .base import SearchHit, get_json, normalize_doi

API = "https://api.openalex.org/works"


def _abstract(inv: dict[str, list[int]] | None) -> str:
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _hit(w: dict[str, Any]) -> SearchHit:
    authors = ", ".join(
        (a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])[:6]
    )
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    oa = w.get("open_access") or {}
    best_oa = w.get("best_oa_location") or {}
    return SearchHit(
        provider="openalex",
        external_id=(w.get("id") or "").rsplit("/", 1)[-1],
        title=w.get("display_name") or w.get("title") or "",
        source_type="paper",
        authors=authors,
        year=w.get("publication_year"),
        venue=src.get("display_name") or "",
        doi=normalize_doi(w.get("doi") or ""),
        url=(w.get("doi") or loc.get("landing_page_url") or w.get("id") or ""),
        abstract=_abstract(w.get("abstract_inverted_index")),
        oa_pdf_url=best_oa.get("pdf_url") or oa.get("oa_url") or "",
        credibility=3,
        license=(best_oa.get("license") or ""),
        meta={
            "cited_by_count": w.get("cited_by_count"),
            "type": w.get("type"),
            "is_oa": oa.get("is_oa"),
            "concepts": [c.get("display_name") for c in (w.get("concepts") or [])[:6]],
        },
    )


def search(query: str, limit: int = 25, year_from: int | None = None, sort: str = "relevance_score:desc") -> list[SearchHit]:
    params: dict[str, Any] = {
        "search": query,
        "per-page": min(max(limit, 1), 100),
        "sort": sort,
        "select": "id,display_name,title,authorships,publication_year,primary_location,best_oa_location,"
                  "open_access,doi,abstract_inverted_index,cited_by_count,type,concepts",
    }
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if filters:
        params["filter"] = ",".join(filters)
    if settings.openalex_mailto:
        params["mailto"] = settings.openalex_mailto
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    data = get_json(API, params=params) or {}
    return [_hit(w) for w in data.get("results") or []]


def get_by_doi(doi: str) -> SearchHit | None:
    params: dict[str, Any] = {}
    if settings.openalex_mailto:
        params["mailto"] = settings.openalex_mailto
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    try:
        data = get_json(f"{API}/https://doi.org/{normalize_doi(doi)}", params=params)
    except Exception:  # noqa: BLE001
        return None
    return _hit(data) if data else None
