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
